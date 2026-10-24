import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.Modifier;
import java.nio.file.*;
import java.util.*;
import java.util.stream.*;

/** Syntax-only dependency/API inventory using the same JDK parser as SourceInventory. */
class DependencyInventory2 {
  static void row(Object... fields) {
    System.out.println(Arrays.stream(fields).map(x -> x == null ? "" : x.toString().replace('\t',' ').replace('\n',' ')).collect(Collectors.joining("\t")));
  }
  public static void main(String[] args) throws Exception {
    JavaCompiler compiler=ToolProvider.getSystemJavaCompiler();
    try(StandardJavaFileManager manager=compiler.getStandardFileManager(null,null,null)) {
      List<Path> paths;
      try(Stream<Path> s=Files.walk(Path.of(args[0]))) { paths=s.filter(p->p.toString().endsWith(".java")).sorted().collect(Collectors.toList()); }
      DiagnosticCollector<JavaFileObject> diagnostics=new DiagnosticCollector<>();
      JavacTask task=(JavacTask)compiler.getTask(null,manager,diagnostics,List.of("-proc:none"),null,manager.getJavaFileObjectsFromPaths(paths));
      Iterable<? extends CompilationUnitTree> units=task.parse();
      SourcePositions positions=Trees.instance(task).getSourcePositions();
      for(CompilationUnitTree unit:units) {
        String original=unit.getSourceFile().getCharContent(true).toString();
        String pkg=unit.getPackageName().toString();
        new TreeScanner<Void,String>() {
          final Map<String,Tree.Kind> kinds=new HashMap<>();
          public Void visitClass(ClassTree cls,String parent) {
            String name=cls.getSimpleName().toString(); if(name.isEmpty())return null;
            String fqn=parent==null?pkg+"."+name:parent+"."+name;
            Set<Modifier> typeFlags=EnumSet.noneOf(Modifier.class);
            typeFlags.addAll(cls.getModifiers().getFlags());
            if(parent!=null && kinds.get(parent)==Tree.Kind.INTERFACE) {
              typeFlags.add(Modifier.PUBLIC);typeFlags.add(Modifier.STATIC);
            }
            kinds.put(fqn,cls.getKind());
            row("T",fqn,pkg,cls.getKind(),typeFlags.stream().map(Object::toString).collect(Collectors.joining(" ")),cls.getTypeParameters().stream().map(Object::toString).collect(Collectors.joining(", ")),cls.getExtendsClause(),cls.getImplementsClause());
            for(ImportTree imp:unit.getImports())row("I",fqn,imp.getQualifiedIdentifier());
            if(cls.getExtendsClause()!=null)row("R",fqn,"super",cls.getExtendsClause());
            for(Tree i:cls.getImplementsClause())row("R",fqn,"super",i);
            for(Tree member:cls.getMembers()) {
              if(member instanceof MethodTree) {
                MethodTree m=(MethodTree)member;
                String category=m.getReturnType()==null?"constructor":"method";
                for(VariableTree p:m.getParameters())row("R",fqn,category,p.getType());
                if(m.getReturnType()!=null)row("R",fqn,"method",m.getReturnType());
                for(Tree t:m.getThrows())row("R",fqn,"method",t);
                Set<Modifier> flags=m.getModifiers().getFlags();
                boolean visible=flags.contains(Modifier.PUBLIC)||flags.contains(Modifier.PROTECTED)||cls.getKind()==Tree.Kind.INTERFACE;
                if(visible) {
                  String mods=flags.stream().map(Object::toString).collect(Collectors.joining(" "));
                  String types=m.getTypeParameters().isEmpty()?"":"<"+m.getTypeParameters().stream().map(Object::toString).collect(Collectors.joining(", "))+"> ";
                  String params=m.getParameters().stream().map(p->{
                    String type=p.getType().toString();
                    String declaration=original.substring((int)positions.getStartPosition(unit,p),(int)positions.getEndPosition(unit,p));
                    if(declaration.contains("...")) {
                      int bracket=type.lastIndexOf("[]");
                      if(bracket<0)throw new IllegalStateException("Varargs without array type: "+declaration);
                      type=type.substring(0,bracket)+"..."+type.substring(bracket+2);
                    }
                    return type+" "+p.getName();
                  }).collect(Collectors.joining(", "));
                  String thrown=m.getThrows().isEmpty()?"":" throws "+m.getThrows().stream().map(Object::toString).collect(Collectors.joining(", "));
                  row("A",fqn,mods+" "+types+(m.getReturnType()==null?name:m.getReturnType()+" "+m.getName())+"("+params+")"+thrown+";");
                }
              } else if(member instanceof VariableTree) {
                VariableTree v=(VariableTree)member;row("R",fqn,"field",v.getType());
                if(v.getModifiers().getFlags().contains(Modifier.PUBLIC)||cls.getKind()==Tree.Kind.INTERFACE)
                  row("A",fqn,v.getModifiers().getFlags().stream().map(Object::toString).collect(Collectors.joining(" "))+" "+v.getType()+" "+v.getName()+";");
              } else if(member instanceof ClassTree)scan(member,fqn);
            }
            return null;
          }
        }.scan(unit,null);
      }
      for(Diagnostic<?> d:diagnostics.getDiagnostics())if(d.getKind()==Diagnostic.Kind.ERROR)throw new IllegalStateException(d.toString());
    }
  }
}
