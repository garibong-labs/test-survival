import java.util.*;
import javax.tools.*;

// Each formatting step can append a diagnostic, including while formatting an error for TSV.
class LateDiagnostics implements Diagnostic<JavaFileObject> {
    final Kind kind;
    final String text;
    final Runnable report;

    LateDiagnostics(Kind kind, String text, Runnable report) {
        this.kind = kind;
        this.text = text;
        this.report = report;
    }

    public String toString() { report.run(); return text; }
    public Kind getKind() { return kind; }
    public String getMessage(Locale locale) { return text; }
    public String getCode() { return ""; }
    public JavaFileObject getSource() { return null; }
    public long getPosition() { return NOPOS; }
    public long getStartPosition() { return NOPOS; }
    public long getEndPosition() { return NOPOS; }
    public long getLineNumber() { return NOPOS; }
    public long getColumnNumber() { return NOPOS; }

    public static void main(String[] args) {
        var diagnostics = new DiagnosticCollector<JavaFileObject>();
        var last = new LateDiagnostics(Kind.ERROR, "last", () -> {});
        var warning = new LateDiagnostics(Kind.WARNING, "warning", () -> diagnostics.report(last));
        diagnostics.report(new LateDiagnostics(Kind.ERROR, "first", new Runnable() {
            int calls;
            public void run() { if (++calls == 2) diagnostics.report(warning); }
        }));
        SnapshotCompiler.formatDiagnostics(diagnostics).forEach(System.out::println);
    }
}
