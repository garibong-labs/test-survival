package reference;

import java.nio.file.*;
import java.util.*;
import javax.tools.*;
import org.apache.maven.plugin.*;

/** Reference-only Maven test-compile goal: preserve duplicate snapshot class names. */
public class SnapshotCompileMojo extends AbstractMojo {
    public void execute() throws MojoExecutionException {
        if (Boolean.getBoolean("ref.skip")) return;
        long started = System.nanoTime();
        try {
            JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
            for (String line : Files.readAllLines(Path.of("compile.tsv"))) {
                String[] p = line.split("\t");
                Path root = Path.of(p[0]), output = Path.of(p[1]);
                Files.createDirectories(output);
                try (var manager = compiler.getStandardFileManager(null, Locale.ROOT, null);
                     var files = Files.walk(root)) {
                    var sources = manager.getJavaFileObjectsFromPaths(files.filter(f -> f.toString().endsWith(".java")).sorted().toList());
                    if (!compiler.getTask(null, manager, null, List.of("-cp",p[2],"-sourcepath",p[0],"-d",p[1]), null, sources).call())
                        throw new IllegalStateException("Frozen sources failed to compile at reference anchor: " + root);
                }
            }
            Files.writeString(Path.of("compile-seconds.txt"), Double.toString((System.nanoTime()-started)/1e9));
        } catch (Exception ex) { throw new MojoExecutionException("Frozen compilation failed", ex); }
    }
}
