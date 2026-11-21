package fixture;

import java.net.URLClassLoader;
import java.util.stream.Stream;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import static org.junit.jupiter.api.Assertions.*;

@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class LoaderTest {
    private static int closedArguments;

    static Stream<Arguments> loaders() {
        return Stream.of(Thread.currentThread().getContextClassLoader(), LoaderTest.class.getClassLoader())
            .map(loader -> Arguments.of(loader, (AutoCloseable) () -> closedArguments++));
    }

    @Order(1) @ParameterizedTest @MethodSource("loaders")
    void autoClose(ClassLoader loader, AutoCloseable argument) {
        assertSame(getClass().getClassLoader(), loader);
        assertEquals(1, Production.hit());
    }

    @Order(2) @Test
    void loadLater() throws Exception {
        assertEquals(2, closedArguments);
        assertEquals(1, Class.forName("fixture.LateHelper").getDeclaredMethod("hit").invoke(null));
    }

    @Order(3) @Test
    void explicitClose() throws Exception {
        ((AutoCloseable) getClass().getClassLoader()).close();
        try (URLClassLoader loader = (URLClassLoader) Thread.currentThread().getContextClassLoader()) {
            assertTrue(loader.getURLs().length > 0);
        }
        URLClassLoader loader = (URLClassLoader) getClass().getClassLoader();
        assertNotNull(loader.findResource("org/junit/jupiter/api/Test.class"));
        System.getProperties().put("fixture.previousLoader", loader);
        assertEquals(1, Production.hit());
    }
}

class LateHelper {
    static int hit() { return Production.hit(); }
}
