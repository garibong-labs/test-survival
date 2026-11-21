import javax.tools.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** javac with source-associated diagnostics, independent of locale/log formatting. */
class SnapshotCompiler {
    public static void main(String[] args) throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        boolean ok;
        try (StandardJavaFileManager files = compiler.getStandardFileManager(diagnostics, Locale.ROOT, StandardCharsets.UTF_8)) {
            var sources = files.getJavaFileObjectsFromStrings(Files.readAllLines(Path.of(args[0])));
            var options = Arrays.asList(Arrays.copyOfRange(args, 2, args.length));
            ok = compiler.getTask(null, files, diagnostics, options, null, sources).call();
        }
        Files.write(Path.of(args[1]), formatDiagnostics(diagnostics));
        System.exit(ok ? 0 : 1);
    }

    static List<String> formatDiagnostics(DiagnosticCollector<JavaFileObject> diagnostics) {
        List<String> errors = new ArrayList<>();
        var entries = diagnostics.getDiagnostics();
        // Formatting can complete javac symbols and append more diagnostics.
        for (int i = 0; i < entries.size(); i++) {
            var diagnostic = entries.get(i);
            System.err.println(diagnostic);
            if (diagnostic.getKind() != Diagnostic.Kind.ERROR) continue;
            String source = diagnostic.getSource() == null ? "" : Path.of(diagnostic.getSource().toUri()).toAbsolutePath().normalize().toString();
            var encoder = Base64.getEncoder();
            errors.add(encoder.encodeToString(source.getBytes(StandardCharsets.UTF_8)) + "\t"
                       + encoder.encodeToString(diagnostic.toString().getBytes(StandardCharsets.UTF_8)));
        }
        return errors;
    }
}
