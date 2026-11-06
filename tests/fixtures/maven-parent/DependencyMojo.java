package fixture;

import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;
import org.apache.maven.model.Dependency;
import java.nio.file.*;
import java.util.*;

/** Offline fixture goals only. Maven itself builds/interpolates the effective
 * parent model; this plugin reports that model and locates preseeded test jars. */
public final class DependencyMojo extends AbstractMojo {
    private MavenProject project;
    private String goal;
    private org.apache.maven.execution.MavenSession session;
    private String property(String name) {
        return session.getUserProperties().getProperty(name, System.getProperty(name));
    }
    public void execute() throws MojoExecutionException {
        try {
            List<String> paths = new ArrayList<>(), listing = new ArrayList<>();
            for (Dependency dependency : project.getDependencies()) {
                if (!dependency.getGroupId().equals("fixture") || !dependency.getArtifactId().equals("library"))
                    throw new IllegalStateException("Unexpected fixture dependency: " + dependency);
                String version = dependency.getVersion();
                Path jar = Path.of(property("maven.repo.local"), "fixture", "library", version, "library-"+version+".jar");
                if (!Files.isRegularFile(jar)) throw new IllegalStateException("Unseeded fixture JAR: " + jar);
                paths.add(jar.toString());
                listing.add("fixture:library:jar:"+version+":test:"+jar);
                if (goal.equals("observe")) Files.writeString(Path.of(property("fixture.output")), version);
            }
            if (paths.size() != 1) throw new IllegalStateException("Expected one effective dependency");
            if (goal.equals("build-classpath")) Files.writeString(Path.of(property("mdep.outputFile")), String.join(":",paths));
            if (goal.equals("list")) Files.writeString(Path.of(property("outputFile")), String.join("\n",listing));
        } catch (Exception failure) { throw new MojoExecutionException("Offline fixture failed", failure); }
    }
}
