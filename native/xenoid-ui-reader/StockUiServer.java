package org.xenoid.qa;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.attribute.PosixFilePermission;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/** A private pipe-only host for the platform's own DumpCommand.run(String[]). */
public final class StockUiServer {
    private static final int MAGIC = 0x58554931; // XUI1
    private static final int MAX_XML_BYTES = 8 * 1024 * 1024;
    private static final int MAX_ERROR_BYTES = 128;
    private static final int QUIT = 127;
    private static final int NAVIGATION = 4;
    private final File directory;
    private final Class<?> commandClass;
    private final Method runMethod;

    private StockUiServer(File directory) throws Exception {
        this.directory = directory;
        if (!directory.isDirectory() || !directory.getCanonicalFile().equals(directory.getAbsoluteFile())) {
            throw new IOException("private_directory_invalid");
        }
        Set<PosixFilePermission> mode = Files.getPosixFilePermissions(directory.toPath());
        for (PosixFilePermission permission : mode) {
            if (permission.name().startsWith("GROUP_") || permission.name().startsWith("OTHERS_")) {
                throw new IOException("private_directory_permissions");
            }
        }
        commandClass = Class.forName("com.android.commands.uiautomator.DumpCommand");
        runMethod = commandClass.getMethod("run", String[].class);
        if (runMethod.getReturnType() != Void.TYPE) throw new IOException("stock_method_signature");
    }

    private byte[] dump(int flags) throws Exception {
        File target = File.createTempFile("ui-", ".xml", directory);
        try {
            List<String> args = new ArrayList<String>();
            if ((flags & 1) != 0) args.add("--compressed");
            if ((flags & 2) != 0) args.add("--windows");
            args.add(target.getAbsolutePath());
            Object command = commandClass.getConstructor().newInstance();
            try {
                runMethod.invoke(command, (Object) args.toArray(new String[args.size()]));
            } catch (InvocationTargetException exception) {
                throw new IOException("stock_dump_exception");
            }
            long size = target.length();
            if (size <= 0) throw new IOException("stock_dump_no_xml");
            if (size > MAX_XML_BYTES) throw new IOException("stock_dump_too_large");
            byte[] data = Files.readAllBytes(target.toPath());
            if (data.length != size || data.length > MAX_XML_BYTES) throw new IOException("stock_dump_size_changed");
            return data;
        } finally {
            // A killed JVM cannot run finally; its fenced host owner also cleans this private directory.
            if (target.exists() && !target.delete()) throw new IOException("stock_dump_cleanup_failed");
        }
    }

    private static Object call(Object value, String method) throws Exception {
        return value.getClass().getMethod(method).invoke(value);
    }

    private static int integer(Object value, String method) throws Exception {
        return ((Integer) call(value, method)).intValue();
    }

    private static String string(Object value, String method) throws Exception {
        Object result = call(value, method);
        return result == null ? "" : result.toString();
    }

    private static boolean flag(Object value, String method) throws Exception {
        return Boolean.TRUE.equals(call(value, method));
    }

    private static int[] rectangle(Object value, String method) throws Exception {
        Class<?> type = Class.forName("android.graphics.Rect");
        Object rect = type.getConstructor().newInstance();
        value.getClass().getMethod(method, type).invoke(value, rect);
        int[] result = new int[4];
        String[] names = {"left", "top", "right", "bottom"};
        for (int i = 0; i < names.length; i++) result[i] = type.getField(names[i]).getInt(rect);
        if (result[0] >= result[2] || result[1] >= result[3]) throw new IOException("nav_invalid_bounds");
        return result;
    }

    private static String rectangleJson(int[] box) {
        return "[" + box[0] + "," + box[1] + "," + box[2] + "," + box[3] + "]";
    }

    private static final class Node {
        final Object value;
        final int depth;
        Node(Object value, int depth) { this.value = value; this.depth = depth; }
    }

    /** Cached metadata only: never query an application root or add an Android read. */
    private static final class NavDiagnostic {
        final StringBuilder displays = new StringBuilder();
        final StringBuilder windows = new StringBuilder();
        int displayCount = -1, windowsSeen = 0, windowsRecorded = 0;
        boolean truncated = false, metadataError = false;

        private static String quoted(CharSequence value, int limit) {
            if (value == null) return "null";
            StringBuilder text = new StringBuilder("\"");
            for (int i = 0; i < value.length() && i < limit; i++) {
                char c = value.charAt(i);
                if (c == '"' || c == '\\') text.append('\\').append(c);
                else if (c >= 32 && c <= 126) text.append(c);
                else {
                    String hex = Integer.toHexString(c);
                    text.append("\\u");
                    for (int j = hex.length(); j < 4; j++) text.append('0');
                    text.append(hex);
                }
            }
            return text.append('"').toString();
        }
        void display(int key, Object value) {
            String type = value == null ? "null" : value.getClass().getSimpleName();
            String row = "{\"key\":" + key + ",\"count\":" + (value instanceof List ? ((List<?>) value).size() : -1)
                    + ",\"value_type\":" + quoted(type, 32) + "}";
            if (displays.length() + row.length() > 1400) { truncated = true; return; }
            if (displays.length() != 0) displays.append(',');
            displays.append(row);
        }
        void window(Object window, int systemType) {
            windowsSeen++;
            if (windowsRecorded >= 12) { truncated = true; return; }
            try {
                int type = integer(window, "getType");
                String row = "{\"id\":" + integer(window, "getId") + ",\"type\":" + type
                        + ",\"display\":" + integer(window, "getDisplayId") + ",\"layer\":" + integer(window, "getLayer");
                if (type == systemType) {
                    Object title = call(window, "getTitle");
                    row += ",\"system_title\":" + quoted(title instanceof CharSequence ? (CharSequence) title : null, 24);
                    if (title instanceof CharSequence && ((CharSequence) title).length() > 24) truncated = true;
                }
                row += "}";
                if (windows.length() + row.length() > 2048) { truncated = true; return; }
                if (windows.length() != 0) windows.append(',');
                windows.append(row); windowsRecorded++;
            } catch (Exception ignored) { metadataError = true; }
        }
        void emit(int sequence, int displayId, String outcome) {
            // The ASCII prefix+row remains below 4096 bytes even with escaped titles.
            try {
                String row = "{\"schema\":\"xenoid.nav-window-diagnostic/v1\",\"sequence\":" + sequence
                        + ",\"display_id\":" + displayId + ",\"display_count\":" + displayCount
                        + ",\"displays\":[" + displays + "],\"windows\":[" + windows + "]"
                        + ",\"windows_seen\":" + windowsSeen + ",\"windows_recorded\":" + windowsRecorded
                        + ",\"truncated\":" + truncated + ",\"metadata_error\":" + metadataError
                        + ",\"outcome\":" + quoted(outcome, 80) + "}";
                String line = "XENOID_NAV_DIAGNOSTIC " + row;
                if (line.length() > 4096) {
                    line = "XENOID_NAV_DIAGNOSTIC {\"schema\":\"xenoid.nav-window-diagnostic/v1\",\"sequence\":"
                            + sequence + ",\"display_id\":" + displayId + ",\"truncated\":true,\"diagnostic_bound\":true}";
                }
                System.err.println(line); System.err.flush();
            } catch (Exception ignored) { /* Observability never changes the proof result. */ }
        }
    }

    /** Rare readonly Back proof in this same serialized JVM, never an app-root dump. */
    private byte[] navigation(int displayId, int sequence) throws Exception {
        NavDiagnostic diagnostic = new NavDiagnostic();
        String diagnosticOutcome = "unknown";
        Object wrapper = null;
        boolean connectionAttempted = false;
        Exception original = null;
        List<Object> nodes = new ArrayList<Object>();
        List<Object> acquiredWindows = new ArrayList<Object>();
        List<?> windows = null;
        try {
            wrapper = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper")
                    .getConstructor().newInstance();
            connectionAttempted = true;
            call(wrapper, "connect");
            Object automation = call(wrapper, "getUiAutomation");
            Object info = call(automation, "getServiceInfo");
            Class<?> infoClass = Class.forName("android.accessibilityservice.AccessibilityServiceInfo");
            int flags = infoClass.getField("flags").getInt(info);
            for (String name : new String[] {"FLAG_RETRIEVE_INTERACTIVE_WINDOWS", "FLAG_REPORT_VIEW_IDS",
                                             "FLAG_INCLUDE_NOT_IMPORTANT_VIEWS"}) {
                flags |= infoClass.getField(name).getInt(null);
            }
            infoClass.getField("flags").setInt(info, flags);
            // Android13 UiAutomation.setServiceInfo clears this connection's
            // AccessibilityInteractionClient cache before sending the flags.
            automation.getClass().getMethod("setServiceInfo", infoClass).invoke(automation, info);
            Object displays = call(automation, "getWindowsOnAllDisplays");
            int displayCount = integer(displays, "size");
            diagnostic.displayCount = displayCount;
            if (displayCount < 0 || displayCount > 16) throw new IOException("nav_window_bound");
            int systemType = Class.forName("android.view.accessibility.AccessibilityWindowInfo")
                    .getField("TYPE_SYSTEM").getInt(null);
            for (int i = 0; i < displayCount; i++) {
                Integer diagnosticKey = null;
                try { diagnosticKey = (Integer) displays.getClass().getMethod("keyAt", int.class).invoke(displays, i); }
                catch (Exception ignored) { diagnostic.metadataError = true; }
                Object values = displays.getClass().getMethod("valueAt", int.class).invoke(displays, i);
                try { if (diagnosticKey != null) diagnostic.display(diagnosticKey.intValue(), values); }
                catch (Exception ignored) { diagnostic.metadataError = true; }
                if (!(values instanceof List)) throw new IOException("nav_display_value_invalid");
                for (Object value : (List<?>) values) {
                    acquiredWindows.add(value);
                    diagnostic.window(value, systemType);
                    if (acquiredWindows.size() > 128) throw new IOException("nav_window_bound");
                }
            }
            Object selected = displays.getClass().getMethod("get", int.class).invoke(displays, displayId);
            if (!(selected instanceof List)) throw new IOException("nav_display_unavailable");
            windows = (List<?>) selected;
            if (windows.size() > 128) throw new IOException("nav_window_bound");
            Object navigation = null;
            for (Object window : windows) {
                if (integer(window, "getType") != systemType || integer(window, "getDisplayId") != displayId
                        || !"Navigation bar".equals(string(window, "getTitle"))) continue;
                if (navigation != null) throw new IOException("nav_window_ambiguous");
                navigation = window;
            }
            if (navigation == null) throw new IOException("nav_bar_unavailable");
            // getRoot(0) bypasses the node cache on Android13 and disables
            // descendant prefetch; no application window's root is requested.
            Object root = navigation.getClass().getMethod("getRoot", int.class).invoke(navigation, 0);
            if (root == null) throw new IOException("nav_root_unavailable");
            ArrayDeque<Node> pending = new ArrayDeque<Node>();
            nodes.add(root);
            pending.add(new Node(root, 0));
            Object back = null;
            int visited = 0;
            while (!pending.isEmpty()) {
                Node node = pending.removeFirst();
                if (++visited > 128 || node.depth > 16) throw new IOException("nav_node_bound");
                if (!flag(node.value, "refresh")) throw new IOException("nav_node_obsolete");
                if ("com.android.systemui:id/back".equals(string(node.value, "getViewIdResourceName"))
                        && "com.android.systemui".equals(string(node.value, "getPackageName"))
                        && flag(node.value, "isVisibleToUser") && flag(node.value, "isEnabled")
                        && flag(node.value, "isClickable")) {
                    if (back != null) throw new IOException("nav_back_ambiguous");
                    back = node.value;
                }
                int count = integer(node.value, "getChildCount");
                if (count < 0 || count > 128 || visited + pending.size() + count > 128
                        || node.depth == 16 && count != 0) throw new IOException("nav_node_bound");
                for (int i = 0; i < count; i++) {
                    Object child = node.value.getClass().getMethod("getChild", int.class, int.class)
                            .invoke(node.value, i, 0);
                    if (child != null) {
                        // Record ownership immediately, including children
                        // still queued when a later provider call fails.
                        nodes.add(child);
                        pending.addLast(new Node(child, node.depth + 1));
                    }
                }
            }
            if (back == null) throw new IOException("nav_back_unavailable");
            // refresh() is an explicit cache-bypassing provider read. Recheck
            // identity and current flags after traversal, not cached labels.
            if (!flag(back, "refresh") || !"com.android.systemui:id/back".equals(string(back, "getViewIdResourceName"))
                    || !"com.android.systemui".equals(string(back, "getPackageName"))
                    || !"com.android.systemui".equals(string(root, "getPackageName"))
                    || !flag(back, "isVisibleToUser") || !flag(back, "isEnabled") || !flag(back, "isClickable")
                    || integer(back, "getWindowId") != integer(navigation, "getId")) {
                throw new IOException("nav_back_changed");
            }
            int[] frame = rectangle(navigation, "getBoundsInScreen");
            int[] box = rectangle(back, "getBoundsInScreen");
            if (!(frame[0] <= box[0] && box[2] <= frame[2] && frame[1] <= box[1] && box[3] <= frame[3])) {
                throw new IOException("nav_back_outside_window");
            }
            String json = "{\"schema\":\"xenoid.nav-node-proof/v1\",\"display_id\":" + displayId
                    + ",\"window_id\":" + integer(navigation, "getId")
                    + ",\"node_window_id\":" + integer(back, "getWindowId")
                    + ",\"window_id_scope\":\"current_ui_automation_connection\""
                    + ",\"window_layer\":" + integer(navigation, "getLayer")
                    + ",\"window_type\":\"TYPE_SYSTEM\",\"window_title\":\"Navigation bar\""
                    + ",\"window_bounds\":" + rectangleJson(frame) + ",\"bounds\":" + rectangleJson(box)
                    + ",\"package\":\"com.android.systemui\",\"resource_id\":\"com.android.systemui:id/back\""
                    + ",\"visible\":true,\"enabled\":true,\"clickable\":true,\"nodes_visited\":" + visited
                    + ",\"cache_bypass\":\"new_connection_setServiceInfo_root_target_refresh\"}";
            byte[] payload = json.getBytes(StandardCharsets.US_ASCII);
            if (payload.length > 16384) throw new IOException("nav_proof_bound");
            diagnosticOutcome = "ok";
            return payload;
        } catch (Exception failure) {
            diagnosticOutcome = failure instanceof IOException ? failure.getMessage() : "nav_metadata_exception";
            original = failure;
            throw failure;
        } finally {
          try {
            IdentityHashMap<Object, Boolean> recycled = new IdentityHashMap<Object, Boolean>();
            for (Object value : nodes) {
                if (recycled.put(value, Boolean.TRUE) == null) {
                    try { call(value, "recycle"); } catch (Exception ignored) { }
                }
            }
            for (Object value : acquiredWindows) {
                try { call(value, "recycle"); } catch (Exception ignored) { }
            }
            if (connectionAttempted) {
                try { call(wrapper, "disconnect"); }
                catch (Exception cleanup) {
                    // A failed wrapper disconnect may leave its handler alive.
                    // Retire this JVM/pipe instead of reporting synchronized
                    // recoverability and accumulating another connection.
                    diagnosticOutcome = "nav_cleanup_failed";
                    throw new IOException("nav_cleanup_failed", original == null ? cleanup : original);
                }
            }
          } finally { diagnostic.emit(sequence, displayId, diagnosticOutcome); }
        }
    }

    private static byte[] identity() throws IOException {
        // PID/starttime let the host validate a cleanup target; they do not authorize a kill by themselves.
        int pid = -1, uid = -1;
        long start = 0;
        File stat = new File("/proc/self/stat");
        if (stat.isFile()) {
            String text = new String(Files.readAllBytes(stat.toPath()), StandardCharsets.US_ASCII);
            pid = Integer.parseInt(text.substring(0, text.indexOf(' ')));
            String[] fields = text.substring(text.lastIndexOf(')') + 2).trim().split("\\s+");
            start = Long.parseLong(fields[19]);
            for (String line : Files.readAllLines(new File("/proc/self/status").toPath(), StandardCharsets.US_ASCII)) {
                if (line.startsWith("Uid:")) uid = Integer.parseInt(line.substring(4).trim().split("\\s+")[0]);
            }
        }
        return ("READY\t" + pid + "\t" + uid + "\t" + start).getBytes(StandardCharsets.US_ASCII);
    }

    private static void reply(DataOutputStream out, int sequence, int status, byte[] data) throws IOException {
        if (data.length > (status == 0 ? MAX_XML_BYTES : MAX_ERROR_BYTES)) throw new IOException("reply_too_large");
        out.writeInt(MAGIC);
        out.writeInt(sequence);
        out.writeByte(status);
        out.writeInt(data.length);
        out.write(data);
        out.flush();
    }

    public static void main(String[] args) throws Exception {
        // Keep all stock informational output away from the framed stdout protocol.
        DataOutputStream out = new DataOutputStream(new FileOutputStream(FileDescriptor.out));
        System.setOut(System.err);
        if (args.length != 1) throw new IOException("one_private_directory_required");
        StockUiServer server = new StockUiServer(new File(args[0]));
        DataInputStream input = new DataInputStream(new FileInputStream(FileDescriptor.in));
        reply(out, 0, 0, identity());
        int previousSequence = 0;
        try {
            while (true) {
                int magic;
                try { magic = input.readInt(); } catch (EOFException end) { break; }
                int sequence = input.readInt();
                int flags = input.readUnsignedByte();
                if (magic != MAGIC || sequence <= previousSequence || sequence <= 0
                        || (flags != QUIT && flags != NAVIGATION && (flags & ~3) != 0)) {
                    reply(out, Math.max(0, sequence), 2, "protocol_rejected".getBytes(StandardCharsets.US_ASCII));
                    break;
                }
                previousSequence = sequence;
                if (flags == QUIT) {
                    reply(out, sequence, 0, "BYE".getBytes(StandardCharsets.US_ASCII));
                    break;
                }
                int displayId = flags == NAVIGATION ? input.readInt() : 0;
                if (displayId < 0) {
                    reply(out, sequence, 2, "protocol_rejected".getBytes(StandardCharsets.US_ASCII));
                    break;
                }
                try {
                    reply(out, sequence, 0, flags == NAVIGATION ? server.navigation(displayId, sequence) : server.dump(flags));
                } catch (Exception failure) {
                    String code = failure instanceof IOException ? failure.getMessage() : "stock_dump_exception";
                    if (code == null || !code.matches("[a-z_]{1,80}")) code = "stock_dump_exception";
                    boolean fatal = "nav_cleanup_failed".equals(code);
                    reply(out, sequence, fatal ? 2 : 1, code.getBytes(StandardCharsets.US_ASCII));
                    if (fatal) break;
                }
            }
        } finally {
            // Every per-request file is already gone before its reply is sent.
            out.flush();
        }
        System.exit(0);
    }
}
