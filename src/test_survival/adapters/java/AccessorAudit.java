import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.Modifier;
import java.nio.file.*;
import java.util.*;
import java.util.stream.*;

/** Syntax-only accessor classification for eligible declared top-level methods. */
class AccessorAudit {
  static boolean accessor(MethodTree m, Set<String> fields, String className) {
    String name=m.getName().toString();
    if (!(name.startsWith("get") || name.startsWith("set") || name.startsWith("is") || name.startsWith("has"))) return false;
    List<? extends StatementTree> body=m.getBody().getStatements();
    if(body.size()==1 && body.get(0) instanceof ReturnTree) return true;
    if(body.size()<1 || body.size()>2 || !(body.get(0) instanceof ExpressionStatementTree)) return false;
    ExpressionTree e=((ExpressionStatementTree)body.get(0)).getExpression();
    if(!(e instanceof AssignmentTree)) return false;
    ExpressionTree lhs=((AssignmentTree)e).getVariable();
    Set<String> parameters=m.getParameters().stream().map(p->p.getName().toString()).collect(Collectors.toSet());
    boolean field=lhs instanceof IdentifierTree && fields.contains(lhs.toString()) && !parameters.contains(lhs.toString());
    if(lhs instanceof MemberSelectTree) {
      MemberSelectTree s=(MemberSelectTree)lhs;
      field=fields.contains(s.getIdentifier().toString()) && Set.of("this",className).contains(s.getExpression().toString());
    }
    return field && (body.size()==1 || body.get(1) instanceof ReturnTree && "this".equals(String.valueOf(((ReturnTree)body.get(1)).getExpression())));
  }
  public static void main(String[] args) throws Exception {
    JavaCompiler compiler=ToolProvider.getSystemJavaCompiler();
    try(StandardJavaFileManager manager=compiler.getStandardFileManager(null,null,null); Stream<Path> stream=Files.walk(Path.of(args[0]))) {
      List<Path> paths=stream.filter(p->p.toString().endsWith(".java")).sorted().collect(Collectors.toList());
      DiagnosticCollector<JavaFileObject> diagnostics=new DiagnosticCollector<>();
      JavacTask task=(JavacTask)compiler.getTask(null,manager,diagnostics,List.of("-proc:none"),null,manager.getJavaFileObjectsFromPaths(paths));
      Iterable<? extends CompilationUnitTree> units=task.parse();
      SourcePositions positions=Trees.instance(task).getSourcePositions();
      System.out.println("class\tmethod\tline\taccessor\tbody");
      for(CompilationUnitTree unit:units) for(Tree type:unit.getTypeDecls()) {
        if(!(type instanceof ClassTree) || type.getKind()!=Tree.Kind.CLASS) continue;
        ClassTree cls=(ClassTree)type;
        String fqn=unit.getPackageName()+"."+cls.getSimpleName();
        Set<String> fields=cls.getMembers().stream().filter(t->t instanceof VariableTree).map(t->((VariableTree)t).getName().toString()).collect(Collectors.toSet());
        for(Tree member:cls.getMembers()) if(member instanceof MethodTree) {
          MethodTree m=(MethodTree)member;
          if(m.getReturnType()==null || m.getBody()==null || m.getModifiers().getFlags().contains(Modifier.PRIVATE) || m.getModifiers().getAnnotations().stream().anyMatch(a->a.getAnnotationType().toString().matches("(?:.*\\.)?Deprecated"))) continue;
          boolean a=accessor(m,fields,cls.getSimpleName().toString());
          System.out.println(fqn+"\t"+m.getName()+"\t"+unit.getLineMap().getLineNumber(positions.getStartPosition(unit,m))+"\t"+a+"\t"+(a?m.getBody().toString().replaceAll("\\s+"," "):""));
        }
      }
      for(Diagnostic<?> d:diagnostics.getDiagnostics()) if(d.getKind()==Diagnostic.Kind.ERROR) throw new IllegalStateException(d.toString());
    }
  }
}
