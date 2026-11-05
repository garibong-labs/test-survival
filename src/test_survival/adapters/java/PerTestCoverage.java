import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.*;
import org.junit.platform.launcher.core.*;
import static org.junit.platform.engine.discovery.DiscoverySelectors.*;
import org.jacoco.cli.internal.core.analysis.*;
import org.jacoco.cli.internal.core.tools.ExecFileLoader;
import org.jacoco.cli.internal.asm.ClassReader;
import org.jacoco.cli.internal.asm.ClassVisitor;
import org.jacoco.cli.internal.asm.Opcodes;

/** One test JVM; isolated class loaders allow different versions of the same test class. */
public final class PerTestCoverage implements TestExecutionListener {
    private final Path output;
    private final Map<String, byte[]> analysisClasses = new LinkedHashMap<>();
    private final Set<String> scope;
    private final boolean dependencies = Boolean.getBoolean("survival.dependencies");
    private final Map<String, Set<String>> setupHits = new HashMap<>();
    private TestPlan plan;
    private int serial;
    private Throwable failure;
    private final Object agent;
    private final java.lang.reflect.Method dump;

    PerTestCoverage(Path output, Path production) throws Exception {
        this.output = output;
        String scopeFile = System.getProperty("survival.coverage.scope");
        scope = scopeFile == null ? null : new HashSet<>(Files.readAllLines(Paths.get(scopeFile)));
        // Production is frozen for this JVM. Read each binary once, and analyze
        // only scope files when collecting survival lines. Probe-based class
        // hits below remain independent and include all instrumented production.
        Path binaryRoot = production.toRealPath();
        try (var paths = Files.walk(binaryRoot, FileVisitOption.FOLLOW_LINKS)) {
            for (Path path : paths.filter(p -> p.toString().endsWith(".class")).sorted().toList()) {
                if (binaryRoot.relativize(path).startsWith("META-INF")) continue;
                byte[] bytes = Files.readAllBytes(path);
                if (scope != null) {
                    ClassReader reader = new ClassReader(bytes);
                    String[] source = new String[1];
                    reader.accept(new ClassVisitor(Opcodes.ASM9) {
                        public void visitSource(String name, String debug) { source[0] = name; }
                    }, ClassReader.SKIP_CODE | ClassReader.SKIP_FRAMES);
                    String name = reader.getClassName();
                    String file = "src/main/java/" + name.substring(0, name.lastIndexOf('/') + 1) + source[0];
                    if (!scope.contains(file)) continue;
                }
                analysisClasses.put(path.toString(), bytes);
            }
        }
        Class<?> rt = ClassLoader.getSystemClassLoader().loadClass("org.jacoco.agent.rt.RT");
        agent = rt.getMethod("getAgent").invoke(null);
        dump = ClassLoader.getSystemClassLoader().loadClass("org.jacoco.agent.rt.IAgent").getMethod("getExecutionData", boolean.class);
    }

    public void testPlanExecutionStarted(TestPlan plan) {
        this.plan = plan;
        try { dump.invoke(agent, true); } catch (Exception ex) { failure = ex; }
    }

    private MethodSource method(TestIdentifier id) {
        TestIdentifier current = id;
        while (true) {
            if (current.getSource().orElse(null) instanceof MethodSource) return (MethodSource) current.getSource().get();
            Optional<TestIdentifier> parent = plan.getParent(current);
            if (!parent.isPresent()) return null;
            current = parent.get();
        }
    }

    private Set<String> hits(byte[] bytes) throws Exception {
        ExecFileLoader data = new ExecFileLoader();
        data.load(new ByteArrayInputStream(bytes));
        Set<String> names = new TreeSet<>();
        for (var entry : data.getExecutionDataStore().getContents())
            for (boolean probe : entry.getProbes()) if (probe) { names.add(entry.getName()); break; }
        return names;
    }

    public synchronized void executionStarted(TestIdentifier id) {
        if (!dependencies || !id.isTest()) return;
        try {
            MethodSource source = method(id);
            if (source != null && !setupHits.containsKey(source.getClassName()))
                setupHits.put(source.getClassName(), hits((byte[])dump.invoke(agent, false)));
        } catch (Throwable ex) { failure = ex; }
    }

    public synchronized void executionFinished(TestIdentifier id, TestExecutionResult result) {
        try {
            byte[] bytes = (byte[]) dump.invoke(agent, true);
            // Container teardown runs after the last test dump. Clear those probes
            // separately so the next test cannot inherit another class's cleanup.
            if (!id.isTest()) return;
            MethodSource source = method(id);
            if (source == null) throw new IllegalStateException("Unmapped test: " + id.getUniqueId());
            String name = Integer.toString(serial++);
            if (scope == null) Files.write(output.resolve(name + ".exec"), bytes);
            if (dependencies) {
                Set<String> names = hits(bytes);
                names.addAll(setupHits.getOrDefault(source.getClassName(), Set.of()));
                Files.write(output.resolve(name + ".classes"), names);
            }
            ExecFileLoader data = new ExecFileLoader();
            data.load(new ByteArrayInputStream(bytes));
            CoverageBuilder coverage = new CoverageBuilder();
            Analyzer analyzer = new Analyzer(data.getExecutionDataStore(), coverage);
            for (var entry : analysisClasses.entrySet()) analyzer.analyzeClass(entry.getValue(), entry.getKey());
            try (BufferedWriter writer = Files.newBufferedWriter(output.resolve(name + ".tsv"))) {
                for (ISourceFileCoverage file : coverage.getSourceFiles()) {
                    String path = "src/main/java/" + (file.getPackageName().isEmpty() ? "" : file.getPackageName() + "/") + file.getName();
                    if (scope != null && !scope.contains(path)) continue;
                    for (int line = file.getFirstLine(); line <= file.getLastLine(); line++) {
                        if (file.getLine(line).getInstructionCounter().getTotalCount() > 0)
                            writer.write(path + "\t" + line + "\t" + (file.getLine(line).getInstructionCounter().getCoveredCount() > 0) + "\n");
                    }
                }
            }
            String message = result.getThrowable().map(Throwable::toString).orElse("");
            String encoded = Base64.getEncoder().encodeToString(message.getBytes(StandardCharsets.UTF_8));
            Files.writeString(output.resolve("tests.tsv"), name + "\t" + source.getClassName() + "\t" + source.getMethodName() + "\t" + result.getStatus() + "\t" + encoded + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (Throwable ex) { failure = ex; }
    }

    public static void batch(String[] args) throws Exception {
        Path out = Paths.get(args[0]); Files.createDirectories(out);
        PerTestCoverage listener = new PerTestCoverage(out, Paths.get(args[1]));
        LauncherDiscoveryRequestBuilder builder = LauncherDiscoveryRequestBuilder.request()
            .configurationParameter("junit.jupiter.execution.parallel.enabled", "false")
            .configurationParameter("junit.vintage.execution.parallel.enabled", "false");
        for (String line : Files.readAllLines(Paths.get(args[2]))) {
            String[] parts = line.split("\t", -1);
            if (parts.length == 1) builder.selectors(selectClass(parts[0]));
            else {
                Class<?> cls = Class.forName(parts[0], false, Thread.currentThread().getContextClassLoader());
                for (java.lang.reflect.Method m : cls.getMethods())
                    if (m.getName().equals(parts[1])) builder.selectors(selectMethod(cls, m));
                for (java.lang.reflect.Method m : cls.getDeclaredMethods())
                    if (!java.lang.reflect.Modifier.isPublic(m.getModifiers()) && m.getName().equals(parts[1])) builder.selectors(selectMethod(cls, m));
            }
        }
        LauncherFactory.create().execute(builder.build(), listener);
        if (listener.failure != null) throw new IllegalStateException("Per-test coverage failed", listener.failure);
    }

    private static void resources(Path source) throws IOException {
        // Each isolated loader also gets its snapshot's filesystem resources.
        // Only scratch src/test/{resources,suite} beneath the worker cwd is touched.
        for (String name : new String[]{"resources", "suite"}) {
            Path target = Paths.get("src/test", name);
            if (Files.exists(target)) {
                try (var paths = Files.walk(target)) {
                    for (Path path : paths.sorted(Comparator.reverseOrder()).toList()) Files.delete(path);
                }
            }
            Path root = source.resolve(name);
            if (Files.exists(root)) {
                try (var paths = Files.walk(root)) {
                    for (Path path : paths.toList()) {
                        Path destination = target.resolve(root.relativize(path));
                        if (Files.isDirectory(path)) Files.createDirectories(destination);
                        else Files.copy(path, destination);
                    }
                }
            }
        }
    }

    /** Analyze only to enumerate executable scope lines, including empty groups.
     * No test discovery, agent, probe dumps, or XML output. */
    private static void universe(String[] args) throws Exception {
        Set<String> files = new HashSet<>(Files.readAllLines(Paths.get(args[3])));
        CoverageBuilder coverage = new CoverageBuilder();
        Analyzer analyzer = new Analyzer(new org.jacoco.cli.internal.core.data.ExecutionDataStore(), coverage);
        try (var roots = Files.list(Paths.get(args[1]))) {
            for (Path root : roots.sorted().toList())
                if (!root.getFileName().toString().equals("META-INF")) analyzer.analyzeAll(root.toFile());
        }
        List<String> lines = new ArrayList<>();
        for (ISourceFileCoverage file : coverage.getSourceFiles()) {
            String path = "src/main/java/" + (file.getPackageName().isEmpty() ? "" : file.getPackageName() + "/") + file.getName();
            if (!files.contains(path)) continue;
            for (int n = file.getFirstLine(); n <= file.getLastLine(); n++)
                if (file.getLine(n).getInstructionCounter().getTotalCount() > 0) lines.add(path + "\t" + n);
        }
        Collections.sort(lines);
        Files.write(Paths.get(args[2]), lines);
    }

    public static void main(String[] args) throws Exception {
        String memoryFile = System.getProperty("survival.memory");
        if (memoryFile != null) Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try {
                long heap = 0, nonheap = 0;
                for (var pool : java.lang.management.ManagementFactory.getMemoryPoolMXBeans()) {
                    long used = pool.getPeakUsage().getUsed();
                    if (pool.getType() == java.lang.management.MemoryType.HEAP) heap += used; else nonheap += used;
                }
                long rss = -1;
                Path status = Path.of("/proc/self/status");
                if (Files.exists(status)) for (String line : Files.readAllLines(status))
                    if (line.startsWith("VmHWM:")) rss = Long.parseLong(line.replaceAll("[^0-9]", "")) * 1024;
                Files.writeString(Path.of(memoryFile), "heap_pool_peak_sum_bytes\t" + heap + "\nnonheap_pool_peak_sum_bytes\t" + nonheap + "\npeak_rss_bytes\t" + rss + "\n");
            } catch (Exception ex) { ex.printStackTrace(); }
        }));
        if (args.length == 4 && args[0].equals("--universe")) {
            universe(args);
            return;
        }
        // Each line: output, production classes, selector file, isolated runtime classpath.
        for (String line : Files.readAllLines(Paths.get(args[0]))) {
            String[] p = line.split("\t", -1);
            if (p.length > 4) resources(Paths.get(p[4]));
            URL[] urls = Arrays.stream(p[3].split(File.pathSeparator)).map(s -> {
                try { return Paths.get(s).toUri().toURL(); } catch (Exception e) { throw new IllegalArgumentException(e); }
            }).toArray(URL[]::new);
            ClassLoader prior = Thread.currentThread().getContextClassLoader();
            try (URLClassLoader loader = new URLClassLoader(urls, ClassLoader.getPlatformClassLoader())) {
                Thread.currentThread().setContextClassLoader(loader);
                loader.loadClass("PerTestCoverage").getMethod("batch", String[].class).invoke(null, (Object) new String[]{p[0], p[1], p[2]});
            } finally { Thread.currentThread().setContextClassLoader(prior); }
        }
    }
}
