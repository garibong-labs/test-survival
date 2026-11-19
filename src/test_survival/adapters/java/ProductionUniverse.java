import com.sun.source.tree.ClassTree;
import com.sun.source.util.JavacTask;
import java.nio.file.*;
import java.util.*;
import javax.tools.ToolProvider;
import org.jacoco.cli.internal.core.analysis.*;
import org.jacoco.cli.internal.core.data.ExecutionDataStore;

/** Match compiled classes to the actual paths of this commit's production sources. */
public final class ProductionUniverse {
    private static Map<String, String> declarations(Path manifest) throws Exception {
        var paths = Files.readAllLines(manifest).stream().filter(s -> !s.isEmpty()).map(Path::of).toList();
        Map<String, String> names = new HashMap<>();
        if (paths.isEmpty()) return names;
        var compiler = ToolProvider.getSystemJavaCompiler();
        try (var files = compiler.getStandardFileManager(null, null, null)) {
            var parser = (JavacTask) compiler.getTask(null, files, null, List.of("-proc:none"), null,
                                                      files.getJavaFileObjectsFromPaths(paths));
            for (var unit : parser.parse()) {
                String pkg = unit.getPackageName() == null ? "" : unit.getPackageName().toString().replace('.', '/') + "/";
                Path path = Path.of("").toAbsolutePath().relativize(Path.of(unit.getSourceFile().toUri()));
                for (var type : unit.getTypeDecls()) {
                    if (type instanceof ClassTree cls) names.put(pkg + cls.getSimpleName(), path.toString());
                }
                if (path.endsWith("package-info.java")) names.put(pkg + "package-info", path.toString());
            }
        }
        return names;
    }

    private static String sourceOf(Map<String, String> names, String binary) {
        // A nested, anonymous or local class shares its top-level declaration's source.
        while (!names.containsKey(binary)) {
            int parent = binary.lastIndexOf('$');
            if (parent <= binary.lastIndexOf('/')) return null;
            binary = binary.substring(0, parent);
        }
        return names.get(binary);
    }

    public static void main(String[] args) throws Exception {
        var names = declarations(Path.of(args[0]));
        Path classes = Path.of(args[1]);
        var coverage = new CoverageBuilder();
        var analyzer = new Analyzer(new ExecutionDataStore(), coverage);
        Path root = classes.toRealPath();
        try (var paths = Files.walk(root, FileVisitOption.FOLLOW_LINKS)) {
            for (Path path : paths.filter(p -> p.toString().endsWith(".class")).sorted().toList()) {
                Path relative = root.relativize(path);
                if (relative.startsWith("META-INF")) continue;
                String binary = relative.toString().replaceFirst("\\.class$", "");
                if (sourceOf(names, binary) != null) analyzer.analyzeAll(path.toFile());
            }
        }
        var binaries = new TreeMap<String, String>();
        var lines = new TreeSet<String>();
        for (var cls : coverage.getClasses()) {
            String file = sourceOf(names, cls.getName());
            // javac -g:lines retains executable lines without a SourceFile attribute.
            // The parsed declaration still identifies the source in that case.
            String sourceName = cls.getSourceFileName();
            if (file == null || (sourceName != null && !Path.of(file).getFileName().toString().equals(sourceName))) continue;
            binaries.put(cls.getName(), file);
            for (int n = cls.getFirstLine(); n <= cls.getLastLine(); n++) {
                if (cls.getLine(n).getInstructionCounter().getTotalCount() > 0) lines.add(file + "\t" + n);
            }
        }
        try (var out = Files.newBufferedWriter(Path.of(args[2]))) {
            for (var entry : binaries.entrySet()) out.write(entry.getKey() + "\t" + entry.getValue() + "\n");
        }
        Files.write(Path.of(args[3]), lines);
    }
}
