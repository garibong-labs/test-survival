package fixture;

import java.net.URLClassLoader;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class NextTest {
    @Test void nextGroup() {
        URLClassLoader previous = (URLClassLoader) System.getProperties().remove("fixture.previousLoader");
        assertNotNull(previous);
        assertNull(previous.findResource("org/junit/jupiter/api/Test.class"));
        assertEquals(1, Production.hit());
    }
}
