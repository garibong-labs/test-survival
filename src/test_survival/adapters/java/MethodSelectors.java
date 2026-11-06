import java.lang.annotation.Annotation;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.stream.Collectors;

/** Inspect compiled test signatures without initializing or rewriting tests. */
public final class MethodSelectors {
    public static void main(String[] args) throws Exception {
        boolean junit4=args.length>1 && args[1].equals("junit4");
        Set<String> tests = new HashSet<>(Arrays.asList("Test", "ParameterizedTest", "RepeatedTest", "TestTemplate", "TestFactory"));
        Class<?> requested = Class.forName(args[0], false, MethodSelectors.class.getClassLoader());
        if(junit4 && Arrays.stream(requested.getAnnotations()).anyMatch(a->a.annotationType().getName().equals("org.junit.Ignore"))) return;
        Set<String> seen = new HashSet<>();
        java.util.List<Method> methods = new java.util.ArrayList<>();
        for (Class<?> type = requested; type != null; type = type.getSuperclass()) {
            Method[] declared = type.getDeclaredMethods();
            Arrays.sort(declared, java.util.Comparator.comparing(Method::isBridge));
            methods.addAll(Arrays.asList(declared));
        }
        Method[] publicMethods = requested.getMethods();
        Arrays.sort(publicMethods, java.util.Comparator.comparing(Method::isBridge));
        methods.addAll(Arrays.asList(publicMethods));
        for (Method method : methods) {
            if (java.lang.reflect.Modifier.isPrivate(method.getModifiers())) continue;
            boolean test = false;
            for (Annotation annotation : method.getDeclaredAnnotations()) {
                if (junit4 ? annotation.annotationType().getName().equals("org.junit.Test") : annotation.annotationType().getName().startsWith("org.junit.") && tests.contains(annotation.annotationType().getSimpleName())) test = true;
            }
            if (junit4 && !test) continue;
            if (!seen.add(method.getName()+Arrays.toString(method.getParameterTypes()))) continue;
            // A bridge shadows the erased superclass method but is not another
            // source test. Real declarations precede same-signature bridges.
            if (method.isBridge() || method.isSynthetic()) continue;
            if(junit4 && Arrays.stream(method.getDeclaredAnnotations()).anyMatch(a->a.annotationType().getName().equals("org.junit.Ignore"))) continue;
            if (test) {
                String parameters = Arrays.stream(method.getParameterTypes()).map(Class::getTypeName).collect(Collectors.joining(","));
                System.out.println(method.getName() + "\t" + requested.getName() + "#" + method.getName() + (junit4?"":"(" + parameters + ")"));
            }
        }
    }
}
