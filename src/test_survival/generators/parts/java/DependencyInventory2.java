import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.TypeElement;
import java.nio.file.*;
import java.util.*;
import java.util.stream.*;
import java.util.regex.Pattern;

/** Ouédraogo-only inventory. The legacy adapter helper is frozen for runner provenance. */
class DependencyInventory2 {
  // Mask comments and literals without changing offsets before inspecting syntax.
  static final Pattern NON_CODE = Pattern.compile(
      "//[^\\r\\n]*|/\\*.*?\\*/|\"\"\".*?\"\"\"|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'", Pattern.DOTALL);
  static String code(String source) {
    return NON_CODE.matcher(source).replaceAll(m -> " ".repeat(m.group().length()));
  }
  static String slice(String source, long start, long end) {
    if(start < 0 || end < start || end > source.length())
      throw new IllegalArgumentException("Unavailable source range ["+start+", "+end+")");
    return source.substring((int)start,(int)end);
  }
  static String parameter(VariableTree p, CompilationUnitTree unit, SourcePositions positions, String source) {
    String type=p.getType().toString();
    long start=positions.getStartPosition(unit,p), end=positions.getEndPosition(unit,p);
    // Compact record constructors have synthetic parameters with no end position.
    String declaration=start>=0 && end>=start && end<=source.length()
        ? slice(source,start,end) : p.toString();
    if(code(declaration).contains("...")) {
      int bracket=type.lastIndexOf("[]");
      if(bracket<0)throw new IllegalArgumentException("Varargs without array type: "+declaration);
      type=type.substring(0,bracket)+"..."+type.substring(bracket+2);
    }
    return type+" "+p.getName();
  }
  static long bodyStart(String source, long start, long end) {
    String header=code(slice(source,start,end));
    int parentheses=0;
    for(int i=0;i<header.length();i++) {
      char c=header.charAt(i);
      if(c=='(')parentheses++;
      else if(c==')')parentheses--;
      else if(c=='{' && parentheses==0)return start+i;
    }
    throw new IllegalArgumentException("Type body not found");
  }
  static String row(Object... fields) {
    return Arrays.stream(fields).map(x -> x == null ? "" : x.toString().replace('\t',' ').replace('\n',' ').replace('\r',' ')).collect(Collectors.joining("\t"));
  }
  /** Resolve names in their declaring compilation unit, including inherited member types and
   * type variables. Only trees used by the signature renderer contribute API references. */
  static void importReferences(CompilationUnitTree unit, Trees trees, Map<ClassTree,String> names,
                               Set<String> project, Set<Tree> signatureTrees) {
    Set<String> imported = new LinkedHashSet<>(), wildcards = new HashSet<>();
    for(ImportTree imp:unit.getImports()) {
      String name=imp.getQualifiedIdentifier().toString();
      if(name.endsWith(".*")) {
        name=name.substring(0,name.length()-2);
        wildcards.add(name);
      } else if(!project.contains(name) && imp.isStatic()) {
        // A static field or method import also names its declaring type.
        name=name.substring(0,name.lastIndexOf('.'));
      }
      if(project.contains(name))imported.add(name);
    }
    Map<String,Set<String>> signatures=new LinkedHashMap<>();
    new TreePathScanner<Void,Boolean>() {
      String owner;
      public Void visitClass(ClassTree cls,Boolean api) {
        String previous=owner;
        // Local/anonymous bodies can use a wildcard import from the enclosing file.
        if(names.containsKey(cls)) {
          owner=names.get(cls);
          signatures.put(owner,new LinkedHashSet<>());
        }
        super.visitClass(cls,false);
        owner=previous;
        return null;
      }
      public Void scan(Tree node,Boolean api) {
        return super.scan(node,api || signatureTrees.contains(node));
      }
      void reference(boolean api) {
        if(owner==null || (!api && wildcards.isEmpty()))return;
        if(trees.getElement(getCurrentPath()) instanceof TypeElement type) {
          String name=type.getQualifiedName().toString();
          if(project.contains(name)) {
            if(api)signatures.get(owner).add(name);
            int dot=name.lastIndexOf('.');
            if(dot>=0 && wildcards.contains(name.substring(0,dot)))imported.add(name);
          }
        }
      }
      public Void visitIdentifier(IdentifierTree id,Boolean api) {
        reference(api);
        return null;
      }
      public Void visitMemberSelect(MemberSelectTree select,Boolean api) {
        reference(api);
        return super.visitMemberSelect(select,api);
      }
    }.scan(unit,false);
    signatures.forEach((owner,refs) -> {
      imported.forEach(t -> System.out.println(row("R",owner,"source-import",t)));
      refs.forEach(t -> System.out.println(row("R",owner,"signature-type",t)));
    });
  }
  public static void main(String[] args) throws Exception {
    JavaCompiler compiler=ToolProvider.getSystemJavaCompiler();
    try(StandardJavaFileManager manager=compiler.getStandardFileManager(null,null,null)) {
      List<Path> paths;
      try(Stream<Path> s=Files.walk(Path.of(args[0]))) { paths=s.filter(p->p.toString().endsWith(".java")).sorted().collect(Collectors.toList()); }
      DiagnosticCollector<JavaFileObject> diagnostics=new DiagnosticCollector<>();
      boolean hints=args.length>1;
      List<String> options=hints ? List.of("-proc:none","-classpath",args[1]) : List.of("-proc:none");
      JavacTask task=(JavacTask)compiler.getTask(null,manager,diagnostics,options,null,manager.getJavaFileObjectsFromPaths(paths));
      Map<ClassTree,String> names=new IdentityHashMap<>();
      Set<Tree> signatureTrees=Collections.newSetFromMap(new IdentityHashMap<>());
      Iterable<? extends CompilationUnitTree> units=task.parse();
      SourcePositions positions=Trees.instance(task).getSourcePositions();
      for(CompilationUnitTree unit:units) {
        String original=unit.getSourceFile().getCharContent(true).toString();
        String pkg=unit.getPackageName()==null?"":unit.getPackageName().toString();
        new TreeScanner<Void,String>() {
          final Map<String,Tree.Kind> kinds=new HashMap<>();
          public Void visitClass(ClassTree cls,String parent) {
            String name=cls.getSimpleName().toString(); if(name.isEmpty())return null;
            String fqn=parent==null?(pkg.isEmpty()?name:pkg+"."+name):parent+"."+name;
            if(hints)names.put(cls,fqn);
            List<String> rows=new ArrayList<>();
            kinds.put(fqn,cls.getKind());
            try {
              long start=positions.getStartPosition(unit,cls), end=positions.getEndPosition(unit,cls);
              long body=bodyStart(original,start,end);
              for(Diagnostic<? extends JavaFileObject> d:diagnostics.getDiagnostics()) {
                if(d.getKind()==Diagnostic.Kind.ERROR && unit.getSourceFile().equals(d.getSource())
                    && d.getPosition()>=start && d.getPosition()<=end)
                  throw new IllegalArgumentException(d.getMessage(Locale.ROOT));
              }
              String components="";
              if(cls.getKind()==Tree.Kind.RECORD) {
                components=cls.getMembers().stream().filter(m -> m instanceof VariableTree
                    && positions.getStartPosition(unit,m)<body).map(m -> parameter((VariableTree)m,unit,positions,original))
                    .collect(Collectors.joining(", "));
              }
              Set<Modifier> typeFlags=EnumSet.noneOf(Modifier.class);
              typeFlags.addAll(cls.getModifiers().getFlags());
              if(parent!=null && (kinds.get(parent)==Tree.Kind.INTERFACE || kinds.get(parent)==Tree.Kind.ANNOTATION_TYPE)) {
                typeFlags.add(Modifier.PUBLIC);typeFlags.add(Modifier.STATIC);
              }
              rows.add(row("T",fqn,pkg,cls.getKind(),typeFlags.stream().map(Object::toString).collect(Collectors.joining(" ")),cls.getTypeParameters().stream().map(Object::toString).collect(Collectors.joining(", ")),cls.getExtendsClause(),cls.getImplementsClause(),components,cls.getPermitsClause()));
              for(ImportTree imp:unit.getImports())rows.add(row("I",fqn,imp.getQualifiedIdentifier()));
              if(cls.getExtendsClause()!=null)rows.add(row("R",fqn,"super",cls.getExtendsClause()));
              for(Tree i:cls.getImplementsClause())rows.add(row("R",fqn,"super",i));
              if(hints) {
                cls.getTypeParameters().forEach(t -> signatureTrees.addAll(t.getBounds()));
                if(cls.getExtendsClause()!=null)signatureTrees.add(cls.getExtendsClause());
                signatureTrees.addAll(cls.getImplementsClause());
                signatureTrees.addAll(cls.getPermitsClause());
              }
              for(Tree member:cls.getMembers()) {
                if(member instanceof MethodTree) {
                  MethodTree m=(MethodTree)member;
                  String category=m.getReturnType()==null?"constructor":"method";
                  for(VariableTree p:m.getParameters())rows.add(row("R",fqn,category,p.getType()));
                  if(m.getReturnType()!=null)rows.add(row("R",fqn,"method",m.getReturnType()));
                  for(Tree t:m.getThrows())rows.add(row("R",fqn,"method",t));
                  Set<Modifier> flags=m.getModifiers().getFlags();
                  boolean visible=flags.contains(Modifier.PUBLIC)||flags.contains(Modifier.PROTECTED)||cls.getKind()==Tree.Kind.INTERFACE||cls.getKind()==Tree.Kind.ANNOTATION_TYPE;
                  if(visible) {
                    if(hints) {
                      m.getTypeParameters().forEach(t -> signatureTrees.addAll(t.getBounds()));
                      if(m.getReturnType()!=null)signatureTrees.add(m.getReturnType());
                      m.getParameters().forEach(p -> signatureTrees.add(p.getType()));
                      signatureTrees.addAll(m.getThrows());
                    }
                    String mods=flags.stream().map(Object::toString).collect(Collectors.joining(" "));
                    String types=m.getTypeParameters().isEmpty()?"":"<"+m.getTypeParameters().stream().map(Object::toString).collect(Collectors.joining(", "))+"> ";
                    String params=m.getParameters().stream().map(p -> parameter(p,unit,positions,original)).collect(Collectors.joining(", "));
                    String thrown=m.getThrows().isEmpty()?"":" throws "+m.getThrows().stream().map(Object::toString).collect(Collectors.joining(", "));
                    rows.add(row("A",fqn,mods+" "+types+(m.getReturnType()==null?name:m.getReturnType()+" "+m.getName())+"("+params+")"+thrown+";"));
                  }
                } else if(member instanceof VariableTree) {
                  VariableTree v=(VariableTree)member;rows.add(row("R",fqn,"field",v.getType()));
                  boolean component=cls.getKind()==Tree.Kind.RECORD && positions.getStartPosition(unit,v)<body;
                  if(hints && component)signatureTrees.add(v.getType());
                  if(v.getModifiers().getFlags().contains(Modifier.PUBLIC)||cls.getKind()==Tree.Kind.INTERFACE||cls.getKind()==Tree.Kind.ANNOTATION_TYPE) {
                    if(hints)signatureTrees.add(v.getType());
                    rows.add(row("A",fqn,v.getModifiers().getFlags().stream().map(Object::toString).collect(Collectors.joining(" "))+" "+v.getType()+" "+v.getName()+";"));
                  }
                }
              }
              rows.forEach(System.out::println);
            } catch(RuntimeException failure) {
              System.out.println(row("S",fqn,unit.getSourceFile().getName(),failure.getClass().getSimpleName()+": "+failure.getMessage()));
            }
            // Nested declarations can still be useful even when their enclosing type failed.
            for(Tree member:cls.getMembers())if(member instanceof ClassTree)scan(member,fqn);
            return null;
          }
        }.scan(unit,null);
      }
      if(hints) {
        // Resolve symbols after saving source-faithful declarations; analysis can rewrite the AST.
        task.analyze();
        Trees trees=Trees.instance(task);
        Set<String> project=new HashSet<>(names.values());
        for(CompilationUnitTree unit:units)importReferences(unit,trees,names,project,signatureTrees);
      }
      for(Diagnostic<?> d:diagnostics.getDiagnostics())if(d.getKind()==Diagnostic.Kind.ERROR)System.out.println(row("D",d.getSource(),d.getLineNumber(),d.getMessage(Locale.ROOT)));
    }
  }
}
