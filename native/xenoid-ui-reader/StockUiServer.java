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
import java.util.List;
import java.util.Set;

/** A private pipe-only host for the platform's own DumpCommand.run(String[]). */
public final class StockUiServer {
    private static final int MAGIC = 0x58554931; // XUI1
    private static final int MAX_XML_BYTES = 8 * 1024 * 1024;
    private static final int MAX_ERROR_BYTES = 128;
    private static final int QUIT = 127;
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
                if (magic != MAGIC || sequence <= previousSequence || sequence <= 0 || (flags != QUIT && (flags & ~3) != 0)) {
                    reply(out, Math.max(0, sequence), 2, "protocol_rejected".getBytes(StandardCharsets.US_ASCII));
                    break;
                }
                previousSequence = sequence;
                if (flags == QUIT) {
                    reply(out, sequence, 0, "BYE".getBytes(StandardCharsets.US_ASCII));
                    break;
                }
                try {
                    reply(out, sequence, 0, server.dump(flags));
                } catch (Exception failure) {
                    String code = failure instanceof IOException ? failure.getMessage() : "stock_dump_exception";
                    if (code == null || !code.matches("[a-z_]{1,80}")) code = "stock_dump_exception";
                    reply(out, sequence, 1, code.getBytes(StandardCharsets.US_ASCII));
                }
            }
        } finally {
            // Every per-request file is already gone before its reply is sent.
            out.flush();
        }
        System.exit(0);
    }
}
