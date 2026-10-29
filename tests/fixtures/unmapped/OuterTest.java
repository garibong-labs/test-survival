package fixture;

import org.junit.jupiter.api.Test;

abstract class OuterTest {
    @Test void inherited() {}

    static class NestedTest extends OuterTest {
        @Test void mapped() {}
        @Test void failing() {}
    }
}
