import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import java.nio.file.*;
import java.util.*;

/** Syntax-only identifiers and declared simple names. Javac handles Unicode escapes. */
class DependencySources {
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        var compiler = ToolProvider.getSystemJavaCompiler();
        var diagnostics = new DiagnosticCollector<JavaFileObject>();
        try (var manager = compiler.getStandardFileManager(null, null, null);
             var paths = Files.walk(root)) {
            var files = paths.filter(p -> p.toString().endsWith(".java")).sorted().toList();
            if (files.isEmpty()) return;
            var task = (JavacTask)compiler.getTask(null, manager, diagnostics, List.of("-proc:none"), null,
                                                  manager.getJavaFileObjectsFromPaths(files));
            for (CompilationUnitTree unit : task.parse()) {
                String file = root.relativize(Path.of(unit.getSourceFile().toUri())).toString();
                Set<String> identifiers = new TreeSet<>(), names = new TreeSet<>();
                new TreeScanner<Void, Void>() {
                    public Void visitIdentifier(IdentifierTree t, Void v) { identifiers.add(t.getName().toString()); return super.visitIdentifier(t,v); }
                    public Void visitMemberSelect(MemberSelectTree t, Void v) { identifiers.add(t.getIdentifier().toString()); return super.visitMemberSelect(t,v); }
                    public Void visitClass(ClassTree t, Void v) { if (!t.getSimpleName().toString().isEmpty()) names.add(t.getSimpleName().toString()); return super.visitClass(t,v); }
                }.scan(unit, null);
                for (String name : names) System.out.println("C\t" + file + "\t" + name);
                for (String name : identifiers) System.out.println("I\t" + file + "\t" + name);
            }
            if (diagnostics.getDiagnostics().stream().anyMatch(d -> d.getKind() == Diagnostic.Kind.ERROR))
                throw new IllegalStateException("Source dependency parse failed: " + diagnostics.getDiagnostics());
        }
    }
}
