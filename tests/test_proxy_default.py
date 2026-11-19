"""Verify Fleet's native proxy bootstrap and private source handling."""

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
PROXY = FLEET["ProxyDefault"]
GLOBALS = PROXY.load.__func__.__globals__
SOURCE = b"socks5://fixture:private-material@example.invalid:1080\n"


def root_owned(info):
    values = list(info)
    values[4:6] = (0, 0)
    return os.stat_result(values)


class Instance:
    root = Path("/fixture/upstream")

    def __init__(self, configured=False, **import_result):
        self.configured = configured
        self.commands = []
        self.import_path = None
        self.import_bytes = None
        self.import_mode = None
        self.import_result = {
            "ok": True, "configured": True, "enabled": True, "quarantined": False,
            "checkCompleted": True, "sourceKind": "endpoint", "udpAllowed": False,
            "ipv6Allowed": True, "allowInsecureHttp": False, **import_result,
        }

    def registry(self):
        return {}

    def require(self, _device, command):
        self.call(command)

    def call(self, command, **kwargs):
        self.commands.append(command)
        status = {"ok": True}
        if command == ["proxy", "status"]:
            assert kwargs.get("capture") is True
            status.update(configured=self.configured, stateReadable=True)
        if command[:2] == ["proxy", "import"]:
            assert kwargs.get("capture") is True
            self.import_path = Path(command[2])
            self.import_mode = stat.S_IMODE(self.import_path.stat().st_mode)
            self.import_bytes = self.import_path.read_bytes()
            status = self.import_result
        return CompletedProcess(command, int(not status["ok"]), json.dumps(status), "private-material")


class ProxyDefaultTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.directory = root / "proxy-default"
        self.directory.mkdir(mode=0o700)
        self.source = self.directory / "source.conf"
        self.source.write_bytes(SOURCE)
        self.source.chmod(0o600)
        self.file = self.directory / "manifest.json"
        self.manifest = {
            "schema": "dev.xenoid.fleet.proxy-default/v1", "kind": "endpoint",
            "sha256": hashlib.sha256(SOURCE).hexdigest(), "udpAllowed": False,
            "ipv6Allowed": True, "allowInsecureHttp": False,
        }
        self.write_manifest()
        original_lstat, original_fstat = Path.lstat, os.fstat
        for context in (
            patch.dict(GLOBALS, {"INSTALL_ROOT": root, "device_lock": lambda _: contextlib.nullcontext()}),
            patch.object(Path, "lstat", lambda path: root_owned(original_lstat(path))),
            patch.object(os, "fstat", lambda fd: root_owned(original_fstat(fd))),
        ):
            context.start()
            self.addCleanup(context.stop)

    def write_manifest(self):
        self.file.write_text(json.dumps(self.manifest))
        self.file.chmod(0o600)

    def test_create_uses_native_import_after_up_and_profile(self):
        instance = Instance()
        FLEET["create_device"](instance, "1", Namespace(no_google_services=False), "/profile.json", None, PROXY.load())
        self.assertEqual(instance.commands[1:4], [
            ["up"], ["device", "apply", "/profile.json", "--instance-identity"], ["proxy", "status"],
        ])
        self.assertEqual(instance.commands[-1], [
            "proxy", "import", str(instance.import_path), "--kind", "endpoint", "--enable", "--no-udp", "--ipv6",
        ])
        self.assertEqual(instance.import_mode, 0o600)
        self.assertEqual(instance.import_bytes, SOURCE)
        self.assertFalse(instance.import_path.exists())
        self.assertNotIn("private-material", repr(PROXY.load()))

    def test_plain_up_retries_default_but_respects_configured_proxy(self):
        for configured in (False, True):
            instance = Instance(configured=configured)
            with self.subTest(configured=configured):
                self.assertEqual(FLEET["run_devices"](instance, ["1"], ["up"]), 0)
                self.assertEqual(instance.commands[:2], [["up"], ["proxy", "status"]])
                self.assertEqual(instance.import_path is None, configured)
        instance = Instance()
        self.assertEqual(FLEET["run_devices"](instance, ["1"], ["up", "--skip-build"]), 0)
        self.assertEqual(instance.commands, [["up", "--skip-build"]])

    def test_unsafe_or_corrupt_source_is_rejected_before_native_up(self):
        instance = Instance()
        self.source.chmod(0o644)
        with self.assertRaisesRegex(FLEET["FleetError"], "private root-owned"):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.source.chmod(0o600)
        self.source.write_bytes(b"different")
        with self.assertRaisesRegex(FLEET["FleetError"], "SHA-256 mismatch"):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.assertEqual(instance.commands, [])

    def test_invalid_schema_and_symlinked_directory_are_rejected(self):
        self.manifest["udpAllowed"] = 0
        self.write_manifest()
        with self.assertRaisesRegex(FLEET["FleetError"], "expected schema"):
            PROXY.load()
        moved = self.directory.with_name("moved")
        self.directory.rename(moved)
        self.assertIsNone(PROXY.load())
        self.directory.symlink_to(moved)
        with self.assertRaisesRegex(FLEET["FleetError"], "private and root-owned"):
            PROXY.load()

    def test_native_error_and_stale_proof_fail_without_echoing_source(self):
        default = PROXY.load()
        for fields in ({"ok": False}, {"checkCompleted": False}, {"quarantined": True}, {"ipv6Allowed": False}):
            instance = Instance(**fields, detail="private-material")
            captured = io.StringIO()
            with self.subTest(fields=fields), contextlib.redirect_stderr(captured):
                with self.assertRaises(FLEET["FleetError"]) as error:
                    default.converge(instance, "1")
            self.assertNotIn("private-material", str(error.exception) + captured.getvalue())
            self.assertFalse(instance.import_path.exists())


if __name__ == "__main__":
    unittest.main()
