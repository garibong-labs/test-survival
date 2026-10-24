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
        for (Class<?> type = requested; type != null; type = type.getSuperclass()) {
            for (Method method : type.getDeclaredMethods()) {
                boolean test = false;
                if(junit4 && Arrays.stream(method.getDeclaredAnnotations()).anyMatch(a->a.annotationType().getName().equals("org.junit.Ignore"))) continue;
                for (Annotation annotation : method.getDeclaredAnnotations()) {
                    if (junit4 ? annotation.annotationType().getName().equals("org.junit.Test") : annotation.annotationType().getName().startsWith("org.junit.") && tests.contains(annotation.annotationType().getSimpleName())) test = true;
                }
                if (test) {
                    String parameters = Arrays.stream(method.getParameterTypes()).map(Class::getTypeName).collect(Collectors.joining(","));
                    System.out.println(method.getName() + "\t" + requested.getName() + "#" + method.getName() + (junit4?"":"(" + parameters + ")"));
                }
            }
        }
    }
}
