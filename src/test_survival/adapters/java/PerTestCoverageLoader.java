import java.io.Closeable;
import java.io.IOException;
import java.net.URL;
import java.net.URLClassLoader;

/** Owns one snapshot's loader for the duration of a runner invocation. */
final class PerTestCoverageLoader implements Closeable {
    private static final class Loader extends URLClassLoader {
        static { registerAsParallelCapable(); }

        Loader(URL[] urls) {
            super(urls, ClassLoader.getPlatformClassLoader());
        }

        @Override public void close() {
            // JUnit and test code may close the context or defining loader.
        }

        private void release() throws IOException {
            super.close();
        }
    }

    private final Loader loader;

    PerTestCoverageLoader(URL[] urls) {
        loader = new Loader(urls);
    }

    URLClassLoader loader() {
        return loader;
    }

    @Override public void close() throws IOException {
        loader.release();
    }
}
