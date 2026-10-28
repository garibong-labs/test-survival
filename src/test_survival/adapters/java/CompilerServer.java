import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import javax.tools.*;

/** Private, sequential protocol. No active processors/plugins or arbitrary options. */
class CompilerServer {
    private static final int MAGIC = 0x43534A31;
    private static String readString(DataInputStream in) throws IOException {
        int size = in.readInt();
        if (size < 0 || size > 1024 * 1024) throw new IOException("Invalid string length");
        byte[] bytes = in.readNBytes(size);
        if (bytes.length != size) throw new EOFException();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    public static void main(String[] unused) throws Exception {
        var in = new DataInputStream(new BufferedInputStream(new FileInputStream(FileDescriptor.in)));
        var protocol = new DataOutputStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out)));
        // Compiler output cannot write to the protocol, even through System.out/err.
        System.setIn(InputStream.nullInputStream());
        PrintStream idle = new PrintStream(OutputStream.nullOutputStream());
        System.setOut(idle);
        System.setErr(idle);
        protocol.writeInt(MAGIC);
        protocol.writeInt(Runtime.version().feature());
        protocol.flush();
        // Registration alone is inactive on JDK 25; callers reject every option or
        // environment setting that could activate a processor. Never assume this
        // policy on an older or future JDK. Other versions use the original CLI.
        if (Runtime.version().feature() != 25) return;
        while (true) {
            int id;
            try { id = in.readInt(); } catch (EOFException end) { return; }
            int count = in.readInt();
            if (count < 5 || count > 8192) throw new IOException("Invalid request length");
            List<String> values = new ArrayList<>();
            for (int i = 0; i < count; i++) values.add(readString(in));
            int code = 2, fault = 0;
            try (PrintStream log = new PrintStream(Files.newOutputStream(Path.of(values.get(0))))) {
                System.setOut(log);
                System.setErr(log);
                try {
                    JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
                    // A fresh task AND manager for every request: no stale classpath/file handles.
                    try (StandardJavaFileManager files = compiler.getStandardFileManager(null, null, null)) {
                        var units = files.getJavaFileObjectsFromStrings(values.subList(4, values.size()));
                        var options = List.of("-cp", values.get(1), "-sourcepath", values.get(2), "-d", values.get(3));
                        var task = compiler.getTask(new PrintWriter(log, true), files, null, options, null, units);
                        code = task.call() ? 0 : 1;
                    }
                } catch (Throwable problem) {
                    problem.printStackTrace(log);
                    fault = 1;
                } finally {
                    System.setOut(idle);
                    System.setErr(idle);
                }
            }
            protocol.writeInt(MAGIC);
            protocol.writeInt(id);
            protocol.writeInt(code);
            protocol.writeInt(fault);
            protocol.flush();
            if (fault != 0) return;
        }
    }
}
