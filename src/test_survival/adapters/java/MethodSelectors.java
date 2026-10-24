import java.lang.annotation.Annotation;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.stream.Collectors;

/** Inspect compiled test signatures without initializing or rewriting tests. */
public final class MethodSelectors {
    public static void main(String[] args) throws Exception {
        Set<String> tests = new HashSet<>(Arrays.asList("Test", "ParameterizedTest", "RepeatedTest", "TestTemplate", "TestFactory"));
        Class<?> requested = Class.forName(args[0], false, MethodSelectors.class.getClassLoader());
        for (Class<?> type = requested; type != null; type = type.getSuperclass()) {
            for (Method method : type.getDeclaredMethods()) {
                boolean test = false;
                for (Annotation annotation : method.getDeclaredAnnotations()) {
                    if (annotation.annotationType().getName().startsWith("org.junit.") && tests.contains(annotation.annotationType().getSimpleName())) test = true;
                }
                if (test) {
                    String parameters = Arrays.stream(method.getParameterTypes()).map(Class::getTypeName).collect(Collectors.joining(","));
                    System.out.println(method.getName() + "\t" + requested.getName() + "#" + method.getName() + "(" + parameters + ")");
                }
            }
        }
    }
}
