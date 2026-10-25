package fixture;
import org.junit.Test;
import java.nio.file.Files;
import java.nio.file.Path;
import java.net.BindException;
import static org.junit.Assert.assertEquals;
public class EnvironmentProbe {
    @Test public void transientBind() throws Exception {
        Path counter=Path.of("environment-attempts.txt");
        int attempt=Files.exists(counter)?Integer.parseInt(Files.readString(counter)):0;
        Files.writeString(counter,Integer.toString(attempt+1));
        if(attempt<2) throw new BindException("fixture environment failure");
        assertEquals(2,Production.value());
    }
}
