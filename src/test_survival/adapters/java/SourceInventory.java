import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.Modifier;
import java.nio.file.*;
import java.util.*;
import java.util.stream.*;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;

/** Syntax-only inventory using the JDK parser: no author source modifications. */
class SourceInventory {
  static String value(Object o) { return o == null ? "" : o.toString().replace("\t", " ").replace("\n", " "); }
  static void row(Object... fields) { System.out.println(Arrays.stream(fields).map(SourceInventory::value).collect(Collectors.joining("\t"))); }
  static String simple(AnnotationTree a) { String s=a.getAnnotationType().toString(); return s.substring(s.lastIndexOf('.')+1); }
  static String display(ModifiersTree mods) {
    for (AnnotationTree a:mods.getAnnotations()) if(simple(a).equals("DisplayName") && !a.getArguments().isEmpty()) {
      ExpressionTree arg=a.getArguments().get(0);
      if(arg instanceof AssignmentTree) arg=((AssignmentTree)arg).getExpression();
      if(arg instanceof LiteralTree) return value(((LiteralTree)arg).getValue());
    }
    return "";
  }
  public static void main(String[] args) throws Exception {
    boolean junit4=args.length>1 && args[1].equals("junit4");
    // Lenient mode (snapshot helper inventory): a unit that does not parse is
    // reported as an "X" row and skipped instead of failing the whole inventory.
    boolean lenient=args.length>2 && args[2].equals("lenient");
    JavaCompiler compiler=ToolProvider.getSystemJavaCompiler();
    StandardJavaFileManager manager=compiler.getStandardFileManager(null,null,null);
    List<Path> paths;
    try(Stream<Path> stream=Files.walk(Path.of(args[0]))) { paths=stream.filter(p->p.toString().endsWith(".java")).sorted().collect(Collectors.toList()); }
    DiagnosticCollector<JavaFileObject> diagnostics=new DiagnosticCollector<>();
    JavacTask task=(JavacTask)compiler.getTask(null,manager,diagnostics,List.of("-proc:none"),null,manager.getJavaFileObjectsFromPaths(paths));
    Iterable<? extends CompilationUnitTree> units=task.parse();
    SourcePositions positions=Trees.instance(task).getSourcePositions();
    Map<String,String> broken=new LinkedHashMap<>();
    for(Diagnostic<? extends JavaFileObject> d:diagnostics.getDiagnostics())
      if(d.getKind()==Diagnostic.Kind.ERROR && d.getSource()!=null) broken.putIfAbsent(d.getSource().toUri().getPath(),d.toString());
    for(CompilationUnitTree unit:units) {
      String path=unit.getSourceFile().toUri().getPath();
      if(lenient && broken.containsKey(path)) { row("X",path,broken.get(path)); continue; }
      String pkg=value(unit.getPackageName());
      String original=unit.getSourceFile().getCharContent(true).toString();
      new TreeScanner<Void, String>() {
        public Void visitClass(ClassTree cls,String parent) {
          String name=cls.getSimpleName().toString();
          if(name.isEmpty()) return null;
          String fqn=parent==null?(pkg.isEmpty()?name:pkg+"."+name):parent+"$"+name;
          boolean top=parent==null;
          boolean ignored=junit4 && cls.getModifiers().getAnnotations().stream().anyMatch(a->simple(a).equals("Ignore"));
          boolean parameterized=junit4 && cls.getModifiers().getAnnotations().stream().anyMatch(a->simple(a).equals("RunWith") && a.toString().contains("Parameterized.class"));
          row("C",fqn,cls.getKind(),top,display(cls.getModifiers()),unit.getSourceFile().toUri().getPath());
          for(Tree member:cls.getMembers()) {
            if(member instanceof MethodTree) {
              MethodTree m=(MethodTree)member;
              if(m.getReturnType()==null) continue;
              List<String> anns=m.getModifiers().getAnnotations().stream().map(SourceInventory::simple).collect(Collectors.toList());
              boolean eligible=top && cls.getKind()==Tree.Kind.CLASS && m.getBody()!=null && !m.getModifiers().getFlags().contains(Modifier.PRIVATE) && !anns.contains("Deprecated");
              String kind=anns.contains("ParameterizedTest")?"parameterized":anns.contains("TestFactory")?"test-factory":(anns.contains("RepeatedTest") || anns.contains("TestTemplate"))?"test-template":anns.contains("Test")?"plain":"";
              if(junit4) {
                boolean test=m.getModifiers().getAnnotations().stream().anyMatch(a->a.getAnnotationType().toString().equals("org.junit.Test") || (a.getAnnotationType().toString().equals("Test") && unit.getImports().stream().anyMatch(i->i.getQualifiedIdentifier().toString().equals("org.junit.Test") || i.getQualifiedIdentifier().toString().equals("org.junit.*"))));
                kind=test && !ignored && !anns.contains("Ignore") ? (parameterized?"parameterized":"plain") : "";
              }
              String sig=m.getName()+"("+m.getParameters().stream().map(p->p.getType().toString()).collect(Collectors.joining(", "))+")";
              String hash="";
              if(m.getBody()!=null) {
                int start=(int)positions.getStartPosition(unit,m.getBody());
                int end=(int)positions.getEndPosition(unit,m.getBody());
                try { hash=HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(original.substring(start+1,end-1).replaceAll("\\s","").getBytes(StandardCharsets.UTF_8))); }
                catch(Exception e) { throw new RuntimeException(e); }
              }
              row("M",fqn,m.getName(),kind,eligible,sig,display(m.getModifiers()),unit.getLineMap().getLineNumber(positions.getStartPosition(unit,m)),hash);
            } else if(member instanceof ClassTree) scan(member,fqn);
          }
          return null;
        }
      }.scan(unit,null);
    }
    for(Diagnostic<?> d:diagnostics.getDiagnostics())
      if(d.getKind()==Diagnostic.Kind.ERROR && !(lenient && d.getSource()!=null)) throw new IllegalStateException(d.toString());
    manager.close();
  }
}
