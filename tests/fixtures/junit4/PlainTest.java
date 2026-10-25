package fixture;
import org.junit.Test;
import org.junit.Ignore;
import static org.junit.Assert.assertEquals;
public class PlainTest {
    @Test public void works() { assertEquals(2, 1+1); }
    @Ignore @Test public void ignored() { throw new AssertionError("must not run"); }
}
