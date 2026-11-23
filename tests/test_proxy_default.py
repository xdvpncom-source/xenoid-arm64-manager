"""Verify Fleet's native proxy bootstrap and private source handling."""

import contextlib
import argparse
import ast
import hashlib
import io
import json
import os
import runpy
import stat
import tempfile
import sys
import types
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch


FLEET = runpy.run_path(str(Path(__file__).resolve().parents[1] / "xenoid-fleet"))
PROXY = FLEET["ProxyDefault"]
GLOBALS = PROXY.load.__func__.__globals__
SOURCE = b"socks5://fixture:private-material@example.invalid:1080\n"


def native_parser():
    """Run the unchanged production parser with inert command dispatch targets."""
    path = Path(__file__).resolve().parents[1] / "nativebundle/source/src/xenoid/cli.py"
    node = next(node for node in ast.parse(path.read_bytes()).body
                if isinstance(node, ast.FunctionDef) and node.name == "build_parser")
    scope = {"argparse": argparse, "Path": Path, "__version__": "fixture",
             "MICROG_PLAY_RELEASE": "fixture", "registered_releases": lambda **kwargs: []}
    scope.update({node.id: None for node in ast.walk(node)
                  if isinstance(node, ast.Name) and node.id.startswith("cmd_")})
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope["build_parser"]()


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

    def test_create_passes_pinned_initial_source_into_up_before_profile(self):
        instance = Instance()
        default = PROXY.load()
        FLEET["create_device"](instance, "1", Namespace(no_google_services=False), "/profile.json", None, default)
        self.assertEqual(instance.commands[1:4], [
            default.initial_up_arguments(), ["device", "apply", "/profile.json", "--instance-identity"], ["proxy", "status"],
        ])
        self.assertIn("--no-initial-proxy-udp", instance.commands[1])
        self.assertIn(hashlib.sha256(SOURCE).hexdigest(), instance.commands[1])
        self.assertNotIn(SOURCE.decode().strip(), instance.commands[1])
        self.assertEqual(instance.commands[-1], [
            "proxy", "import", str(instance.import_path), "--kind", "endpoint", "--enable", "--no-udp", "--ipv6",
        ])
        self.assertEqual(instance.import_mode, 0o600)
        self.assertEqual(instance.import_bytes, SOURCE)
        self.assertFalse(instance.import_path.exists())
        self.assertNotIn("private-material", repr(PROXY.load()))

    def test_optional_default_flags_keep_legacy_manifest_valid_and_capture_explicit_choices(self):
        original = PROXY.load()
        self.assertTrue(original.enabled)
        self.assertEqual(original.selected_node, "")
        self.manifest.update(selectedNode="saved-node", enabled=False)
        self.write_manifest()
        updated = PROXY.load()
        command = updated.initial_up_arguments()
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.selected_node, "saved-node")
        self.assertIn("--no-initial-proxy-enable", command)
        self.assertIn("--initial-proxy-name=saved-node", command)
        self.manifest["enabled"] = 0
        self.write_manifest()
        with self.assertRaises(FLEET["FleetError"]): PROXY.load()

    def test_selected_node_roundtrips_actual_native_parser_including_option_like_names(self):
        parser = native_parser()
        for name in ("", "saved-node", "-UK", "--UK"):
            with self.subTest(name=name):
                default = PROXY(SOURCE, "endpoint", False, True, False, selected_node=name)
                parsed = parser.parse_args(default.initial_up_arguments())
                self.assertEqual(parsed.initial_proxy_name, name)
                self.assertFalse(parsed.initial_proxy_udp)
                self.assertTrue(parsed.initial_proxy_ipv6)
                instance = Instance(selectedNode=name)
                default.converge(instance, "1")
                imported = parser.parse_args(instance.commands[-1])
                self.assertEqual(imported.name or "", name)

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

    def test_continuous_import_uses_native_route_proof_without_external_check(self):
        helper = Mock(return_value=True)
        package = types.ModuleType("xenoid"); package.__path__ = []
        module = types.ModuleType("xenoid.daemon_client"); module.proxy_routing_ready = helper
        instance = Instance(checkCompleted=False, routingReady=True,
                            failurePolicy={"mode": "retry_proxy"})
        with patch.dict(sys.modules, {"xenoid": package, "xenoid.daemon_client": module}):
            PROXY.load().converge(instance, "1")
        helper.assert_called_once_with(instance.import_result)
        self.assertFalse(any(command == ["proxy", "status", "--check"] for command in instance.commands))
        self.assertEqual(instance.import_bytes, SOURCE)

    def test_continuous_import_cannot_fall_back_to_a_stale_legacy_check(self):
        package = types.ModuleType("xenoid"); package.__path__ = []
        for routing_ready, native_ready in [(False, True), (True, False)]:
            helper = Mock(return_value=native_ready)
            module = types.ModuleType("xenoid.daemon_client"); module.proxy_routing_ready = helper
            instance = Instance(checkCompleted=True, routingReady=routing_ready,
                                failurePolicy={"mode": "retry_proxy"})
            with patch.dict(sys.modules, {"xenoid": package, "xenoid.daemon_client": module}):
                with self.assertRaises(FLEET["FleetError"]): PROXY.load().converge(instance, "1")

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
