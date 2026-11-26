"""Exercise the real Java installer method without an Android runtime."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "nativebundle/source/daemon/app/src/main/java/dev/xenoid/daemon/RootHelper.java"


def method_source(source, name):
    """Extract the real small static method, keeping Java string literals intact."""
    found = re.search(r"\bstatic\s+[^;{}\n]+\b" + re.escape(name)
                      + r"\s*\([^)]*\)\s*\{", source)
    if found is None:
        raise AssertionError("Missing native method " + name)
    depth, quote, escaped = 1, None, False
    for offset in range(found.end(), len(source)):
        char = source[offset]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[found.start():offset + 1]
    raise AssertionError("Unterminated native method " + name)


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "JDK is required")
class NativeAppInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        source = SOURCE.read_text()
        methods = "\n".join(method_source(source, name) for name in ("installApk", "shellQuote"))
        harness = r'''
import java.util.*;
import java.nio.charset.StandardCharsets;
public class NativeInstallerHarness {
    static String captured;
    static int calls;
    static Map<String,Object> supplied = new HashMap<>();
    static Map<String,Object> exec(String command) {
        captured = command;
        calls++;
        return supplied;
    }
METHODS
    public static void main(String[] args) {
        supplied.put("ok", Boolean.valueOf(args[1]));
        supplied.put("exit", Integer.valueOf(args[2]));
        supplied.put("stdout", args[3]);
        Map<String,Object> returned = installApk(args[0]);
        if (returned != supplied) throw new AssertionError("Root result was replaced");
        System.out.println(Base64.getEncoder().encodeToString(captured.getBytes(StandardCharsets.UTF_8)));
        System.out.println(returned.get("ok"));
        System.out.println(returned.get("exit"));
        System.out.println(calls);
    }
}
'''.replace("METHODS", methods)
        path = cls.directory / "NativeInstallerHarness.java"
        path.write_text(harness)
        subprocess.run(["javac", "-encoding", "UTF-8", "-d", str(cls.directory), str(path)],
                       check=True, capture_output=True, text=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def exercise(self, path, ok=True, exit_code=0, stdout="Success"):
        result = subprocess.run(["java", "-cp", str(self.directory), "NativeInstallerHarness",
                                 path, str(ok).lower(), str(exit_code), stdout],
                                check=True, capture_output=True, text=True, timeout=15)
        command, returned_ok, returned_exit, calls = result.stdout.splitlines()
        self.assertEqual(calls, "1", "A mutating install must not be resent")
        return base64.b64decode(command).decode(), returned_ok, returned_exit

    def test_official_install_uses_replace_and_vending_installer_flags(self):
        command, ok, exit_code = self.exercise("/data/local/tmp/xenoid-install.apk")
        self.assertEqual(command, "pm install -r -i com.android.vending '/data/local/tmp/xenoid-install.apk'")
        self.assertEqual((ok, exit_code), ("true", "0"))

    def test_path_shell_metacharacters_remain_one_argument(self):
        path = "/data/local/tmp/odd 'name; $(touch MUST_NOT_EXIST) `touch ALSO_NOT_EXIST`\nfile.apk"
        command, _, _ = self.exercise(path)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            binary = directory / "pm"
            binary.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            binary.chmod(0o755)
            result = subprocess.run(["/bin/sh", "-c", command], cwd=directory,
                                    env={**os.environ, "PATH": str(directory)},
                                    check=True, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), ["install", "-r", "-i", "com.android.vending", path])
            self.assertEqual(sorted(item.name for item in directory.iterdir()), ["pm"])

    def test_failed_install_and_timeout_results_are_returned_without_success(self):
        for code, output in ((1, "Failure [INSTALL_FAILED_INVALID_APK]"),
                             (124, "command timed out")):
            with self.subTest(code=code):
                _, ok, exit_code = self.exercise("/data/local/tmp/xenoid-install.apk", False, code, output)
                self.assertEqual((ok, exit_code), ("false", str(code)))


if __name__ == "__main__":
    unittest.main()
