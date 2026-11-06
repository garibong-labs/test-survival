import java.nio.file.*;
import java.net.*;
import java.util.*;
import org.junit.Test;

/** One Surefire test fork, with the worker's snapshot/resource isolation. */
public class FrozenSnapshotTest {
    @Test public void frozenMethods() throws Exception {
        long started = System.nanoTime();
        List<String> evidence = new ArrayList<>();
        for (String line : Files.readAllLines(Path.of("run.tsv"))) {
            String[] p = line.split("\t");
            for (String name : List.of("resources", "suite")) {
                Path target = Path.of("src/test", name), source = Path.of(p[2], name);
                if (Files.exists(target)) try (var paths = Files.walk(target)) {
                    for (Path f : paths.sorted(Comparator.reverseOrder()).toList()) Files.delete(f);
                }
                if (Files.exists(source)) try (var paths = Files.walk(source)) {
                    for (Path f : paths.toList()) {
                        Path to = target.resolve(source.relativize(f));
                        if (Files.isDirectory(f)) Files.createDirectories(to); else Files.copy(f,to);
                    }
                }
            }
            URL[] urls = Arrays.stream(p[1].split(":" )).map(s -> {
                try { return Path.of(s).toUri().toURL(); } catch (Exception ex) { throw new IllegalArgumentException(ex); }
            }).toArray(URL[]::new);
            ClassLoader previous = Thread.currentThread().getContextClassLoader();
            try (var loader = new URLClassLoader(urls, ClassLoader.getPlatformClassLoader())) {
                Thread.currentThread().setContextClassLoader(loader);
                evidence.add(p[0]+"\t"+loader.loadClass("FrozenRunner").getMethod("run",String.class).invoke(null,p[0]));
            } finally { Thread.currentThread().setContextClassLoader(previous); }
        }
        Files.write(Path.of("observations.tsv"),evidence);
        Files.writeString(Path.of("test-seconds.txt"),Double.toString((System.nanoTime()-started)/1e9));
        Files.writeString(Path.of("surefire-pid.txt"),Long.toString(ProcessHandle.current().pid()));
    }
}
