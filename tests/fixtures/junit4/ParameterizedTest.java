package fixture;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.Parameterized;
import static org.junit.Assert.assertTrue;
@RunWith(Parameterized.class)
public class ParameterizedTest {
    @Parameterized.Parameters public static Object[][] values() { return new Object[][] {{1}, {2}}; }
    private final int value;
    public ParameterizedTest(int value) { this.value=value; }
    @Test public void works() { assertTrue(value>0); }
}
