import java.io.*;
import java.lang.instrument.*;
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
import org.jacoco.cli.internal.asm.ClassWriter;
import org.jacoco.cli.internal.asm.MethodVisitor;
import org.jacoco.cli.internal.asm.Opcodes;

/** One test JVM; isolated class loaders allow different versions of the same test class. */
public final class PerTestCoverage implements TestExecutionListener {
    private static volatile PerTestCoverage current;
    private final Path output;
    private final Map<String, byte[]> analysisClasses;
    private final Set<String> scope;
    private final boolean dependencies = Boolean.getBoolean("survival.dependencies");
    private final Map<String, Set<String>> setupHits = new HashMap<>();
    private Set<String> discoveryHits = Set.of();
    private final Set<String> initializerHits = new TreeSet<>();
    private int initializing;
    private boolean initializerUnbounded;
    private TestPlan plan;
    private int serial;
    private final boolean isolatedMethod = Boolean.getBoolean("survival.coverage.isolateMethods");
    private String serialPrefix = "";
    private Throwable failure;
    private final Object agent;
    private final java.lang.reflect.Method dump;

    /** Runs after JaCoCo's transformer, preserving its original class IDs and
     * probes. Hooks neither reset probes nor change the frozen class files. */
    public static final class Initializers implements ClassFileTransformer {
        private final Set<Path> roots = new HashSet<>();
        private final Set<String> classes = new HashSet<>();

        public static void premain(String args, Instrumentation instrumentation) throws Exception {
            Initializers transformer = new Initializers();
            for (String root : System.getProperty("survival.initializer.roots").split(File.pathSeparator))
                transformer.roots.add(Path.of(root).toRealPath());
            for (Path root : transformer.roots) try (var files = Files.walk(root)) {
                for (Path file : files.filter(p -> Files.isRegularFile(p) && p.toString().endsWith(".class")).toList()) {
                    String name = root.relativize(file).toString().replace(File.separatorChar, '/');
                    transformer.classes.add(name.substring(0, name.length() - 6));
                }
            }
            instrumentation.addTransformer(transformer);
            System.setProperty("survival.initializers", "true");
        }

        public byte[] transform(ClassLoader loader, String name, Class<?> redefining,
                                java.security.ProtectionDomain domain, byte[] bytes) {
            try {
                if (!classes.contains(name)) return null;
                if (domain == null || domain.getCodeSource() == null ||
                    !domain.getCodeSource().getLocation().getProtocol().equals("file") ||
                    !roots.contains(Path.of(domain.getCodeSource().getLocation().toURI()).toRealPath())) {
                    System.setProperty("survival.initializer.failure", "true");
                    return null;
                }
                ClassReader reader = new ClassReader(bytes);
                ClassWriter writer = new ClassWriter(reader, 0);
                boolean[] changed = {false};
                reader.accept(new ClassVisitor(Opcodes.ASM9, writer) {
                    public MethodVisitor visitMethod(int access, String method, String descriptor, String signature, String[] exceptions) {
                        MethodVisitor delegate = super.visitMethod(access, method, descriptor, signature, exceptions);
                        if (!method.equals("<clinit>")) return delegate;
                        changed[0] = true;
                        return new MethodVisitor(Opcodes.ASM9, delegate) {
                            private void hook(String method) {
                                super.visitMethodInsn(Opcodes.INVOKESTATIC, "PerTestCoverage", method, "()V", false);
                            }
                            public void visitCode() { super.visitCode(); hook("initializerStarted"); }
                            public void visitInsn(int opcode) {
                                if (opcode == Opcodes.RETURN) hook("initializerFinished");
                                if (opcode == Opcodes.ATHROW) hook("initializerFailed");
                                super.visitInsn(opcode);
                            }
                        };
                    }
                }, 0);
                if (!changed[0]) return null;
                // Custom test loaders need not expose our bridge. Leave their
                // bytecode intact and conservatively invalidate attribution.
                if (!Boolean.TRUE.equals(Class.forName("PerTestCoverage", false, loader)
                        .getMethod("hasDependencyListener").invoke(null))) {
                    System.setProperty("survival.initializer.failure", "true");
                    return null;
                }
                return writer.toByteArray();
            } catch (Throwable ex) {
                // A transformer failure must not change the tests' behavior or
                // silently leave their dependency evidence bounded.
                System.setProperty("survival.initializer.failure", "true");
                return null;
            }
        }
    }

    private synchronized void initializer(boolean finished, boolean failed) {
        if (!dependencies) return;
        if (!finished) { initializing++; return; }
        initializerUnbounded |= failed;
        try {
            // Retain a conservative superset: probes already hit in the current
            // interval may also run in the initializer, so subtraction is unsafe.
            initializerHits.addAll(hits((byte[])dump.invoke(agent, false)));
        } catch (Throwable ex) { initializerUnbounded = true; }
        if (--initializing < 0) initializerUnbounded = true;
    }

    public static void initializerStarted() { if (current != null) current.initializer(false, false); }
    public static void initializerFinished() { if (current != null) current.initializer(true, false); }
    public static void initializerFailed() { if (current != null) current.initializer(true, true); }
    public static boolean hasDependencyListener() { return current != null; }

    PerTestCoverage(Path output, Map<String, byte[]> analysisClasses) throws Exception {
        this.output = output;
        this.analysisClasses = analysisClasses;
        scope = readScope();
        Class<?> rt = ClassLoader.getSystemClassLoader().loadClass("org.jacoco.agent.rt.RT");
        agent = rt.getMethod("getAgent").invoke(null);
        dump = ClassLoader.getSystemClassLoader().loadClass("org.jacoco.agent.rt.IAgent").getMethod("getExecutionData", boolean.class);
    }

    private static Set<String> readScope() throws IOException {
        String scopeFile = System.getProperty("survival.coverage.scope");
        return scopeFile == null ? null : new HashSet<>(Files.readAllLines(Paths.get(scopeFile)));
    }

    private static Map<String, byte[]> loadClasses(Path production, Set<String> scope) throws IOException {
        // Production is frozen for this JVM. Read each binary once, and analyze
        // only scope files when collecting survival lines. Probe-based class
        // hits below remain independent and include all instrumented production.
        Map<String, byte[]> classes = new LinkedHashMap<>();
        Path binaryRoot = production.toRealPath();
        try (var paths = Files.walk(binaryRoot, FileVisitOption.FOLLOW_LINKS)) {
            for (Path path : paths.filter(p -> Files.isRegularFile(p) && p.toString().endsWith(".class")).sorted().toList()) {
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
                classes.put(path.toString(), bytes);
            }
        }
        return Collections.unmodifiableMap(classes);
    }

    public void testPlanExecutionStarted(TestPlan plan) {
        this.plan = plan;
        if (isolatedMethod) return; // retain discovery probes for this method
        try {
            byte[] before = (byte[])dump.invoke(agent, true);
            // Vintage parameter providers can execute production during discovery.
            // Their dependencies support every method discovered in this snapshot.
            if (dependencies) discoveryHits = hits(before);
        } catch (Exception ex) { failure = ex; }
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
        if (!dependencies) return;
        try {
            // A container starts before its setup. Its first child starts after
            // that setup, before any child can finish and reset the probes.
            // Keep hits by container identity so nested siblings inherit shared
            // ancestor setup without inheriting another sibling's test body.
            Optional<TestIdentifier> parent = plan.getParent(id);
            if (parent.isPresent() && !setupHits.containsKey(parent.get().getUniqueId()))
                setupHits.put(parent.get().getUniqueId(), hits((byte[])dump.invoke(agent, false)));
        } catch (Throwable ex) { failure = ex; }
    }

    public synchronized void executionFinished(TestIdentifier id, TestExecutionResult result) {
        try {
            byte[] bytes = isolatedMethod ? null : (byte[]) dump.invoke(agent, true);
            // Container teardown runs after the last test dump. Clear those probes
            // separately so the next test cannot inherit another class's cleanup.
            if (!id.isTest()) return;
            MethodSource source = method(id);
            if (source == null) throw new IllegalStateException("Unmapped test: " + id.getUniqueId());
            String name = serialPrefix + serial++;
            if (dependencies) {
                Set<String> names = hits(bytes);
                names.addAll(discoveryHits);
                names.addAll(initializerHits);
                Optional<TestIdentifier> parent = plan.getParent(id);
                while (parent.isPresent()) {
                    names.addAll(setupHits.getOrDefault(parent.get().getUniqueId(), Set.of()));
                    parent = plan.getParent(parent.get());
                }
                Files.write(output.resolve(name + ".classes"), names);
                if (!Boolean.getBoolean("survival.initializers") || initializerUnbounded || initializing != 0 ||
                    Boolean.getBoolean("survival.initializer.failure"))
                    Files.writeString(output.resolve(name + ".unbounded"), "Incomplete class-initializer attribution");
            }
            if (!isolatedMethod) writeCoverage(name, bytes);
            String message = result.getThrowable().map(Throwable::toString).orElse("");
            String encoded = Base64.getEncoder().encodeToString(message.getBytes(StandardCharsets.UTF_8));
            Files.writeString(output.resolve("tests.tsv"), name + "\t" + source.getClassName() + "\t" + source.getMethodName() + "\t" + result.getStatus() + "\t" + encoded + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (Throwable ex) { failure = ex; }
    }

    private void writeCoverage(String name, byte[] bytes) throws Exception {
        if (scope == null) Files.write(output.resolve(name + ".exec"), bytes);
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
    }

    public static void batch(String[] args) throws Exception {
        batch(args, loadClasses(Paths.get(args[1]), readScope()));
    }

    public static void batch(String[] args, Map<String, byte[]> classes) throws Exception {
        Path out = Paths.get(args[0]); Files.createDirectories(out);
        PerTestCoverage listener = new PerTestCoverage(out, classes);
        current = listener;
        if (listener.isolatedMethod) {
            listener.serialPrefix = args[4] + "-";
            listener.dump.invoke(listener.agent, true);
        }
        LauncherDiscoveryRequestBuilder builder = LauncherDiscoveryRequestBuilder.request()
            .configurationParameter("junit.jupiter.execution.parallel.enabled", "false")
            .configurationParameter("junit.vintage.execution.parallel.enabled", "false");
        for (String line : listener.isolatedMethod ? List.of(args[3]) : Files.readAllLines(Paths.get(args[2]))) {
            String[] parts = line.split("\t", -1);
            if (parts.length == 1) builder.selectors(selectClass(parts[0]));
            else {
                Class<?> cls = Class.forName(parts[0], false, Thread.currentThread().getContextClassLoader());
                // Include inherited package/protected Jupiter methods as well as
                // public Vintage methods, while respecting overriding methods.
                Set<String> seen = new HashSet<>();
                for (Class<?> type = cls; type != null; type = type.getSuperclass()) {
                    java.lang.reflect.Method[] declared = type.getDeclaredMethods();
                    Arrays.sort(declared, Comparator.comparing(java.lang.reflect.Method::isBridge));
                    for (java.lang.reflect.Method m : declared) {
                        String signature = m.getName()+Arrays.toString(m.getParameterTypes());
                        if (!seen.add(signature) || java.lang.reflect.Modifier.isPrivate(m.getModifiers())) continue;
                        // Retain the erased signature for ancestor shadowing,
                        // without selecting the compiler-generated bridge.
                        if (m.isBridge() || m.isSynthetic()) continue;
                        if (m.getName().equals(parts[1])) builder.selectors(selectMethod(cls, m));
                    }
                }
                java.lang.reflect.Method[] publicMethods = cls.getMethods();
                Arrays.sort(publicMethods, Comparator.comparing(java.lang.reflect.Method::isBridge));
                for (java.lang.reflect.Method m : publicMethods)
                    if (seen.add(m.getName()+Arrays.toString(m.getParameterTypes())) && !m.isBridge() && !m.isSynthetic()
                            && m.getName().equals(parts[1]))
                        builder.selectors(selectMethod(cls, m));
            }
        }
        LauncherFactory.create().execute(builder.build(), listener);
        if (listener.isolatedMethod) {
            // A source method includes all invocations, discovery, setup and
            // teardown. Analyze the cumulative probes only after its plan ends.
            byte[] bytes = (byte[])listener.dump.invoke(listener.agent, true);
            for (int i = 0; i < listener.serial; i++) listener.writeCoverage(listener.serialPrefix + i, bytes);
        }
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
        Map<Path, Map<String, byte[]>> bytecode = new HashMap<>();
        Set<String> selectedScope = readScope();
        for (String line : Files.readAllLines(Paths.get(args[0]))) {
            String[] p = line.split("\t", -1);
            Path production = Paths.get(p[1]).toRealPath();
            if (!bytecode.containsKey(production)) bytecode.put(production, loadClasses(production, selectedScope));
            if (p.length > 4) resources(Paths.get(p[4]));
            URL[] urls = Arrays.stream(p[3].split(File.pathSeparator)).map(s -> {
                try { return Paths.get(s).toUri().toURL(); } catch (Exception e) { throw new IllegalArgumentException(e); }
            }).toArray(URL[]::new);
            boolean isolate = Boolean.getBoolean("survival.coverage.isolateMethods");
            List<String> selections = isolate ? Files.readAllLines(Paths.get(p[2])) : Collections.singletonList(null);
            int selector = 0;
            for (String selection : selections) {
                ClassLoader prior = Thread.currentThread().getContextClassLoader();
                try (URLClassLoader loader = new URLClassLoader(urls, ClassLoader.getPlatformClassLoader())) {
                    Thread.currentThread().setContextClassLoader(loader);
                    String[] arguments = isolate ? new String[]{p[0], p[1], p[2], selection, Integer.toString(selector++)}
                                                 : new String[]{p[0], p[1], p[2]};
                    loader.loadClass("PerTestCoverage").getMethod("batch", String[].class, Map.class)
                        .invoke(null, arguments, bytecode.get(production));
                } finally { Thread.currentThread().setContextClassLoader(prior); }
            }
        }
        // Tests may leave non-daemon threads behind (schedulers, executors). Without an explicit
        // exit the JVM waits for them forever after every result has been written.
        System.exit(0);
    }
}
