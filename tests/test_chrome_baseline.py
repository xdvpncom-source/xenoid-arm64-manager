"""Protect baseline validation and native creation/recreation command order."""

import contextlib
import hashlib
import io
import json
import os
import runpy
import stat
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


FLEET = runpy.run_path(str(Path(__file__).resolve().parents[1] / "xenoid-fleet"))
BASELINE = FLEET["ChromeBaseline"]
FLEET_GLOBALS = BASELINE.load.__func__.__globals__


class Instance:
    root = Path("/fixture/upstream")

    def __init__(self, existing=False, installed=None):
        self.existing = existing
        self.installed = installed
        self.commands = []

    def registry(self):
        return {"1": {"ok": True}} if self.existing else {}

    def require(self, _device, command):
        self.commands.append(command)

    def recreate(self, _device):
        self.commands.append(["start", "--recreate", "--no-wait"])

    def document(self, command, _device):
        self.commands.append(command)
        output = f"package:com.android.chrome versionCode:{self.installed}\n" if self.installed is not None else ""
        return {"ok": True, "stdout": output}, 0

    def call(self, command, **_options):
        self.commands.append(command)
        if command[:2] == ["adb", "install-multiple"]:
            self.installed = 720418033
        return CompletedProcess(command, 0, "", "")


def root_owned(info):
    data = list(info)
    data[4:6] = (0, 0)
    return os.stat_result(data)


class ChromeBaselineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "chrome-baseline"
        self.directory.mkdir(mode=0o700)
        self.enterContext(patch.dict(FLEET_GLOBALS, {"INSTALL_ROOT": self.root}))
        manifest = {
            "schema": "dev.xenoid.fleet.chrome-baseline/v1",
            "version_code": 720418033,
            "chrome": [self.apk("base.apk"), self.apk("split_config.en.apk")],
            "trichrome": self.apk("trichrome.apk"),
        }
        self.manifest = self.directory / "manifest.json"
        self.manifest.write_text(json.dumps(manifest))
        self.manifest.chmod(0o600)
        original_lstat, original_fstat = Path.lstat, os.fstat
        self.enterContext(patch.object(Path, "lstat", lambda path: root_owned(original_lstat(path))))
        self.enterContext(patch.object(os, "fstat", lambda fd: root_owned(original_fstat(fd))))

    def apk(self, filename):
        data = filename.encode()
        path = self.directory / filename
        path.write_bytes(data)
        path.chmod(0o600)
        return {"file": filename, "sha256": hashlib.sha256(data).hexdigest()}

    def test_create_and_recreate_install_after_native_up_and_profile(self):
        baseline = BASELINE.load()
        for existing in (False, True):
            with self.subTest(existing=existing), contextlib.redirect_stderr(io.StringIO()):
                instance = Instance(existing=existing)
                FLEET["create_device"](instance, "1", Namespace(no_google_services=False), "/profile.json", baseline)
                self.assertEqual(instance.commands[0][0], "start" if existing else "init")
                self.assertEqual(instance.commands[1:3], [["up"], ["device", "apply", "/profile.json", "--instance-identity"]])
                self.assertEqual(instance.commands[4], ["app", "install", str(self.directory / "trichrome.apk")])
                self.assertEqual(instance.commands[5], ["adb", "install-multiple", "-r", str(self.directory / "base.apk"),
                                                        str(self.directory / "split_config.en.apk")])
                self.assertEqual(instance.commands[3], instance.commands[6])

    def test_plain_up_converges_once_and_preserves_equal_or_later_versions(self):
        for version in (720418033, 720418034):
            instance = Instance(installed=version)
            with self.subTest(version=version), patch.dict(FLEET_GLOBALS, {"device_lock": lambda _device: contextlib.nullcontext()}):
                self.assertEqual(FLEET["run_devices"](instance, ["1"], ["up"]), 0)
            self.assertEqual(len(instance.commands), 2)
            self.assertEqual(instance.commands[0], ["up"])
            self.assertEqual(instance.commands[1][:4], ["adb", "shell", "pm", "list"])

    def test_mismatched_hash_and_insecure_modes_fail_before_device_routes(self):
        base = self.directory / "base.apk"
        base.chmod(0o644)
        with self.assertRaisesRegex(FLEET["FleetError"], "private root-owned"):
            BASELINE.load()
        base.chmod(0o600)
        base.write_bytes(b"changed")
        with self.assertRaisesRegex(FLEET["FleetError"], "SHA-256 mismatch"):
            BASELINE.load()

    def test_symlinked_baseline_is_rejected_and_missing_baseline_is_optional(self):
        replacement = self.root / "moved"
        self.directory.rename(replacement)
        self.assertIsNone(BASELINE.load())
        self.directory.symlink_to(replacement)
        self.assertTrue(stat.S_ISLNK(self.directory.lstat().st_mode))
        with self.assertRaisesRegex(FLEET["FleetError"], "private and root-owned"):
            BASELINE.load()

    def test_present_baseline_requires_a_valid_manifest_before_up(self):
        self.manifest.unlink()
        instance = Instance()
        with self.assertRaises(FileNotFoundError):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.manifest.write_text("invalid json")
        self.manifest.chmod(0o600)
        with self.assertRaisesRegex(FLEET["FleetError"], "invalid JSON"):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.assertEqual(instance.commands, [])


if __name__ == "__main__":
    unittest.main()
