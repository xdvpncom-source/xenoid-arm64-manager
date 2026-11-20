"""Verify host rollback opt-out and original resource backup selection."""
import ast
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "qa/restore-display.py").read_text())
functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                            and node.name in {"disable_resource_defaults", "find_original", "resource_apply_lock"}],
                       type_ignores=[])


class HostRollbackTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "resources.json"
        self.state = self.root / "state"
        self.backup = self.root / "backup"
        self.backup.mkdir()
        self.namespace = {"RESOURCE_CONFIG": self.config, "RESOURCE_STATE": self.state,
                          "Path": Path, "json": json, "tempfile": tempfile, "os": os,
                          "contextmanager": contextmanager, "fcntl": fcntl}
        exec(compile(functions, str(ROOT / "qa/restore-display.py"), "exec"), self.namespace)

    def test_apply_disables_defaults_and_preserves_all_admin_fields_with_backup(self):
        original = {"enabled": True, "fps": 30, "scale": None, "custom": {"keep": True}}
        raw = json.dumps(original).encode()
        self.config.write_bytes(raw)
        result = self.namespace["disable_resource_defaults"](self.backup)
        self.assertEqual(json.loads(self.config.read_text()), {**original, "enabled": False})
        self.assertEqual((self.backup / "qa-resources-before.json").read_bytes(), raw)
        self.assertTrue(result["changed"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_legacy_host_does_not_gain_new_configuration(self):
        result = self.namespace["disable_resource_defaults"](self.backup)
        self.assertEqual(result, {"changed": False, "status": "not-installed"})
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_disabled_profile_is_idempotent_and_original_backup_is_kept(self):
        self.config.write_text('{"enabled":true,"fps":20}')
        disable = self.namespace["disable_resource_defaults"]
        disable(self.backup)
        before = (self.backup / "qa-resources-before.json").read_bytes()
        self.assertEqual(disable(self.backup)["status"], "already-disabled")
        self.assertEqual((self.backup / "qa-resources-before.json").read_bytes(), before)

    def test_new_addon_original_display_precedes_fallback_profile(self):
        self.state.mkdir()
        original = self.state / "phone-2-original-display.json"
        original.write_text(json.dumps({"size": [1440, 3120], "density": 560}))
        def no_transport(*_args):
            raise AssertionError("Native profile should not replace the saved original metrics")
        metrics, rate, evidence = self.namespace["find_original"](2, no_transport, self.backup)
        self.assertEqual(metrics, {"width": 1440, "height": 3120, "density": 560})
        self.assertIsNone(rate)
        self.assertIn(str(original), evidence)

    def test_preview_does_not_create_resource_lock_or_change_config(self):
        self.config.write_text('{"enabled":true,"fps":20}')
        with self.namespace["resource_apply_lock"](2, False):
            self.assertFalse(self.state.exists())
        self.assertTrue(json.loads(self.config.read_text())["enabled"])

    def test_apply_holds_same_per_device_lock_as_timer(self):
        self.config.write_text('{"enabled":true,"fps":20}')
        with self.namespace["resource_apply_lock"](2, True):
            with open(self.state / "phone-2.lock", "a") as second:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_reused_numeric_id_prefers_identified_new_original_over_unknown_legacy(self):
        identity = {"instance_id": "new-instance", "resource_tag": "new-tag"}
        self.state.mkdir()
        (self.state / "phone-2-original-display.json").write_text(json.dumps(
            {"size": [1080, 1920], "density": 420, "identity": identity, "identity_binding": "captured"}))
        (self.backup / "display-before.json").write_text(json.dumps([
            {"device_id": 2, "size_before": "Physical size: 1440x3120",
             "density_before": "Physical density: 560"}]))
        metrics, _rate, _sources = self.namespace["find_original"](
            2, lambda *_args: self.fail("Identified backup should be sufficient"), self.backup, identity=identity)
        self.assertEqual(metrics, {"width": 1080, "height": 1920, "density": 420})

    def test_foreign_immutable_legacy_and_addon_records_do_not_override_current_profile(self):
        identity = {"instance_id": "current-instance", "resource_tag": "current-tag"}
        foreign = {"instance_id": "foreign-instance", "resource_tag": "foreign-tag"}
        self.state.mkdir()
        (self.state / "phone-2-original-display.json").write_text(json.dumps(
            {"size": [1440, 3120], "density": 560, "identity": foreign, "identity_binding": "captured"}))
        (self.backup / "display-before.json").write_text(json.dumps([
            {"device_id": 2, "identity": foreign, "size_before": "Physical size: 1440x3120",
             "density_before": "Physical density: 560"}]))
        execute = lambda *_args: SimpleNamespace(stdout=json.dumps(
            {"display": {"width": 1080, "height": 1920, "densityDpi": 420}}).encode())
        metrics, _rate, _sources = self.namespace["find_original"](2, execute, self.backup, identity=identity)
        self.assertEqual(metrics, {"width": 1080, "height": 1920, "density": 420})

    def test_migrated_legacy_addon_keeps_earliest_known_original_for_current_fleet(self):
        identity = {"instance_id": "current-instance", "resource_tag": "current-tag"}
        self.state.mkdir()
        (self.state / "phone-2-original-display.json").write_text(json.dumps(
            {"size": [200, 356], "density": 72, "identity": identity, "identity_binding": "legacy"}))
        (self.backup / "display-before.json").write_text(json.dumps([
            {"device_id": 2, "identity": identity, "size_before": "Physical size: 1440x3120",
             "density_before": "Physical density: 560"}]))
        # effective_metrics is normally loaded alongside find_original on host.
        self.namespace["effective_metrics"] = lambda _size, _density: {"width": 1440, "height": 3120, "density": 560}
        metrics, _rate, _sources = self.namespace["find_original"](
            2, lambda *_args: self.fail("Saved legacy backup should be sufficient"), self.backup, identity=identity)
        self.assertEqual(metrics, {"width": 1440, "height": 3120, "density": 560})
