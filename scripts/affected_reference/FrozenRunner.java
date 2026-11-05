import java.nio.file.*;
import java.util.*;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.*;
import org.junit.platform.launcher.core.*;
import org.junit.platform.launcher.listeners.SummaryGeneratingListener;
import static org.junit.platform.engine.discovery.DiscoverySelectors.*;

/** Ordinary JUnit execution in one isolated frozen snapshot, without coverage analysis. */
public class FrozenRunner {
    public static String run(String selectors) throws Exception {
        var builder = LauncherDiscoveryRequestBuilder.request()
            .configurationParameter("junit.jupiter.execution.parallel.enabled", "false")
            .configurationParameter("junit.vintage.execution.parallel.enabled", "false");
        Set<String> expected = new HashSet<>(), observed = new HashSet<>();
        for (String line : Files.readAllLines(Path.of(selectors))) {
            String[] p = line.split("\t"); expected.add(p[0]+"#"+p[1]);
            Class<?> cls = Class.forName(p[0], false, Thread.currentThread().getContextClassLoader());
            for (var method : cls.getMethods()) if (method.getName().equals(p[1])) builder.selectors(selectMethod(cls, method));
            for (var method : cls.getDeclaredMethods())
                if (!java.lang.reflect.Modifier.isPublic(method.getModifiers()) && method.getName().equals(p[1])) builder.selectors(selectMethod(cls, method));
        }
        var summary = new SummaryGeneratingListener();
        LauncherFactory.create().execute(builder.build(), summary, new TestExecutionListener() {
            private TestPlan plan;
            public void testPlanExecutionStarted(TestPlan plan) { this.plan = plan; }
            public void executionFinished(TestIdentifier id, org.junit.platform.engine.TestExecutionResult result) {
                if (!id.isTest()) return;
                while (!(id.getSource().orElse(null) instanceof MethodSource)) {
                    if (plan.getParent(id).isEmpty()) return;
                    id = plan.getParent(id).get();
                }
                MethodSource source = (MethodSource)id.getSource().get();
                observed.add(source.getClassName()+"#"+source.getMethodName());
            }
        });
        var result = summary.getSummary();
        if (!observed.equals(expected) || result.getTotalFailureCount() > 0 || result.getTestsAbortedCount() > 0)
            throw new IllegalStateException("Reference differs: expected="+expected+" observed="+observed+" failures="+result.getFailures());
        return expected.size()+"\t"+result.getTestsSucceededCount();
    }
}
