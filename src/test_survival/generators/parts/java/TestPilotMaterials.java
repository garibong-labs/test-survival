import com.sun.source.doctree.DocCommentTree;
import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.stream.*;

/** Read the selected top-level method declarations with the same positions as SourceInventory. */
class TestPilotMaterials {
    static String encode(String text) {
        return Base64.getEncoder().encodeToString(text.getBytes(StandardCharsets.UTF_8));
    }

    static String header(CompilationUnitTree unit, MethodTree method, String source, SourcePositions positions) {
        int start = (int) positions.getStartPosition(unit, method);
        int end = (int) positions.getStartPosition(unit, method.getBody());
        StringBuilder result = new StringBuilder(source.substring(start, end));
        List<int[]> annotations = new ArrayList<>();
        new TreeScanner<Void, Void>() {
            public Void visitBlock(BlockTree block, Void unused) { return null; }
            public Void visitAnnotation(AnnotationTree annotation, Void unused) {
                annotations.add(new int[]{(int) positions.getStartPosition(unit, annotation) - start,
                                          (int) positions.getEndPosition(unit, annotation) - start});
                return null;
            }
        }.scan(method, null);
        annotations.sort(Comparator.comparingInt((int[] range) -> range[0]).reversed());
        for (int[] range : annotations) result.delete(range[0], range[1]);
        return result.toString().strip();
    }

    static String javadoc(DocTrees docs, CompilationUnitTree unit, MethodTree method, String source, int start) {
        DocCommentTree comment = docs.getDocCommentTree(TreePath.getPath(unit, method));
        if (comment == null) return "";
        int pos = (int) docs.getSourcePositions().getStartPosition(unit, comment, comment);
        if (pos >= 0) {
            int line = source.lastIndexOf('\n', pos - 1) + 1;
            if (source.substring(line, pos).stripLeading().startsWith("///")) {
                int end = source.indexOf('\n', pos);
                while (end >= 0 && source.substring(end + 1).stripLeading().startsWith("///")) {
                    end = source.indexOf('\n', end + 1);
                }
                return source.substring(line, end < 0 ? source.length() : end).strip();
            }
        }
        int opening = source.lastIndexOf("/**", pos < 0 ? start : pos);
        int closing = source.indexOf("*/", opening + 2);
        return source.substring(opening, closing + 2);
    }

    public static void main(String[] args) throws Exception {
        Set<String> wanted = new LinkedHashSet<>(Files.readAllLines(Path.of(args[1])));
        if (wanted.isEmpty()) return;
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        try (var manager = compiler.getStandardFileManager(null, null, StandardCharsets.UTF_8);
             var files = Files.walk(Path.of(args[0]))) {
            var sources = files.filter(p -> p.toString().endsWith(".java")).sorted().toList();
            if (sources.isEmpty()) throw new IllegalArgumentException("Unmatched methods.csv row: " + wanted.iterator().next());
            var task = (JavacTask) compiler.getTask(null, manager, null, List.of("-proc:none"), null,
                                                    manager.getJavaFileObjectsFromPaths(sources));
            DocTrees docs = DocTrees.instance(task);
            SourcePositions positions = docs.getSourcePositions();
            for (var unit : task.parse()) {
                String source = unit.getSourceFile().getCharContent(true).toString();
                for (Tree type : unit.getTypeDecls()) {
                    if (!(type instanceof ClassTree owner)) continue;
                    String pkg = unit.getPackageName() == null ? "" : unit.getPackageName() + ".";
                    String fqn = pkg + owner.getSimpleName();
                    for (Tree member : owner.getMembers()) {
                        if (!(member instanceof MethodTree method) || method.getBody() == null) continue;
                        int start = (int) positions.getStartPosition(unit, method);
                        String signature = method.getName() + "(" + method.getParameters().stream()
                            .map(p -> p.getType().toString()).collect(Collectors.joining(", ")) + ")";
                        String key = fqn + "\t" + unit.getLineMap().getLineNumber(start) + "\t" + signature;
                        if (!wanted.remove(key)) continue;
                        int end = (int) positions.getEndPosition(unit, method);
                        System.out.println("M\t" + key + "\t" + encode(header(unit, method, source, positions))
                            + "\t" + encode(source.substring(start, end))
                            + "\t" + encode(javadoc(docs, unit, method, source, start)));
                    }
                }
            }
        }
        if (!wanted.isEmpty()) throw new IllegalArgumentException("Unmatched methods.csv row: " + wanted.iterator().next());
    }
}
