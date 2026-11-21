import java.nio.file.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class WritesTest {
    @Test void writes() throws Exception {
        var resource = Path.of(getClass().getResource("/script.sml").toURI());
        var output = resource.resolveSibling("script.sml.out");
        assertFalse(Files.exists(output), "found output from a previous execution");
        Files.writeString(output, "generated");
        var input = Path.of("src/test/input/data");
        assertEquals("original\n", Files.readString(input));
        Files.writeString(input, "rewritten");
        assertThrows(java.io.IOException.class, () ->
            Files.writeString(Path.of("/experiment/dev/src-test-t/resources/direct"), "forbidden"));
        assertTrue(Production.version() >= 0);
    }
}
