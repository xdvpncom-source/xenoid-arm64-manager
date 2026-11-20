"""Check administrator resource settings and physical display calculations."""

import contextlib
import fcntl
import hashlib
import json
import math
import runpy
import unittest
import tempfile
import threading
import types
from unittest.mock import Mock, patch
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "qa" / "qa_resources.py"))
validate_config = MODULE["validate_config"]
desired_metrics = MODULE["desired_metrics"]
apply_metrics = MODULE["apply_metrics"]
reconcile = MODULE["reconcile"]
GLOBALS = reconcile.__globals__


class ResourceConfigurationTest(unittest.TestCase):
    def defaults(self, **overrides):
        return validate_config({
            "enabled": True, "fps": 20, "scale": "minimum", "min_side": 200,
            "dpi": 72, "timeout": 180, **overrides,
        })

    def physical(self, size=(720, 1280), density=560, override_size=None,
                 override_density=None):
        return {
            "physical_size": size, "physical_density": density,
            "override_size": override_size, "override_density": override_density,
            "size": override_size or size, "density": override_density or density,
        }

    def test_minimum_keeps_physical_aspect_ratio_and_even_dimensions(self):
        self.assertEqual(desired_metrics(self.physical(), self.defaults()),
                         ((200, 356), 72, False))
        self.assertEqual(desired_metrics(self.physical((1280, 720)), self.defaults()),
                         ((356, 200), 72, False))

    def test_minimum_uses_physical_not_previous_override(self):
        before = self.physical(override_size=(1080, 1920), override_density=420)
        self.assertEqual(desired_metrics(before, self.defaults()), ((200, 356), 72, False))

    def test_odd_minimum_rounds_up_to_even(self):
        result = desired_metrics(self.physical(), self.defaults(min_side=201))
        self.assertEqual(result[0][0], 202)
        self.assertTrue(all(value % 2 == 0 for value in result[0]))

    def test_native_scale_keeps_exact_odd_physical_values(self):
        before = self.physical((721, 1281), 561, (200, 356), 72)
        self.assertEqual(desired_metrics(before, self.defaults(scale=1)),
                         ((721, 1281), 561, True))

    def test_numeric_scale_uses_physical_density(self):
        before = self.physical(override_size=(200, 356), override_density=72)
        self.assertEqual(desired_metrics(before, self.defaults(scale=0.5)),
                         ((360, 640), 280, False))

    def test_null_scale_preserves_existing_metrics(self):
        self.assertIsNone(desired_metrics(self.physical(), self.defaults(scale=None)))

    def test_administrator_values_are_configurable(self):
        config = self.defaults(fps=30, min_side=320, dpi=120, timeout=0)
        self.assertEqual(config["fps"], 30)
        self.assertEqual(config["timeout"], 0)
        self.assertEqual(desired_metrics(self.physical(), config), ((320, 568), 120, False))
        self.assertEqual(self.defaults(fps=0)["fps"], 0)
        self.assertIsNone(self.defaults(fps=None)["fps"])

    def test_invalid_known_values_fail_validation(self):
        invalid = {
            "enabled": (0, 1, "true", None),
            "fps": (True, -1, 1.5, "20", math.inf),
            "scale": (True, False, 0, -0.5, math.inf, math.nan, "small"),
            "min_side": (True, 0, -1, 1.5, "200"),
            "dpi": (True, 0, -1, 1.5, "72"),
            "timeout": (True, -1, 1.5, "180", None),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.defaults(**{key: value})

    def test_unknown_setting_is_reported_instead_of_ignored(self):
        with self.assertRaises(ValueError):
            self.defaults(frmae_rate=30)

    def test_configuration_must_be_an_object(self):
        for value in (None, [], "minimum", 20):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(value)

    def test_invalid_physical_values_fail_before_mutation(self):
        for before in (self.physical((0, 1280)), self.physical((-720, 1280)),
                       self.physical(density=0), self.physical((720, math.inf))):
            with self.subTest(before=before), self.assertRaises((ValueError, RuntimeError)):
                desired_metrics(before, self.defaults())

    def test_numeric_overflow_is_rejected(self):
        with self.assertRaises((ValueError, OverflowError)):
            desired_metrics(self.physical(), self.defaults(scale=1e308))


class AndroidDisplay:
    """A stateful wm transport, including partial application before rejection."""

    def __init__(self, *, fail=None, invalid=False):
        self.physical_size = (720, 1280)
        self.physical_density = 560
        self.size = (1080, 1920)
        self.density = 420
        self.fail = fail
        self.invalid = invalid
        self.writes = []

    def owned_call(self, command, timeout):
        assert command[:2] == ["shell", "wm"]
        kind = command[2]
        if len(command) == 3:
            if kind == "size":
                physical = (0, 1280) if self.invalid else self.physical_size
                stdout = f"Physical size: {physical[0]}x{physical[1]}\n"
                if self.size is not None:
                    stdout += f"Override size: {self.size[0]}x{self.size[1]}\n"
            else:
                stdout = f"Physical density: {self.physical_density}\n"
                if self.density is not None:
                    stdout += f"Override density: {self.density}\n"
            return {"ok": True, "stdout": stdout}
        value = command[3]
        self.writes.append((kind, value))
        changed = None if value == "reset" else (
            tuple(map(int, value.split("x"))) if kind == "size" else int(value))
        setattr(self, kind, changed)
        rejected = self.fail == (kind, value)
        return {"ok": not rejected, "stdout": ""}


class ResourceApplyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.backup = self.root / "original.json"
        self.config = validate_config({})

    def test_readback_confirms_both_settings_and_retains_original(self):
        broker = AndroidDisplay()
        result = apply_metrics(broker, self.config, self.backup)
        self.assertEqual(result, {"changed": True, "size": (200, 356), "density": 72})
        self.assertEqual(broker.writes, [("size", "200x356"), ("density", "72")])
        original = json.loads(self.backup.read_text())
        self.assertEqual(original["override_size"], [1080, 1920])
        self.assertEqual(original["override_density"], 420)
        apply_metrics(broker, self.config, self.backup)
        self.assertEqual(len(broker.writes), 2)
        self.assertEqual(json.loads(self.backup.read_text()), original)

    def test_second_rejected_write_restores_both_attempts(self):
        broker = AndroidDisplay(fail=("density", "72"))
        with self.assertRaisesRegex(RuntimeError, "rejected display density"):
            apply_metrics(broker, self.config, self.backup)
        self.assertEqual(broker.writes, [
            ("size", "200x356"), ("density", "72"),
            ("density", "420"), ("size", "1080x1920"),
        ])
        self.assertEqual((broker.size, broker.density), ((1080, 1920), 420))

    def test_failed_first_rollback_still_restores_remaining_setting(self):
        for failure in ("rejected", "exception"):
            with self.subTest(failure=failure):
                broker = AndroidDisplay(fail=("density", "72"))
                original_call = broker.owned_call

                def reject_restore(command, timeout):
                    result = original_call(command, timeout)
                    if command == ["shell", "wm", "density", "420"]:
                        if failure == "exception":
                            raise OSError("transport unavailable")
                        return {"ok": False}
                    return result

                broker.owned_call = reject_restore
                with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
                    apply_metrics(broker, self.config, self.backup)
                self.assertEqual(broker.writes[-2:], [("density", "420"), ("size", "1080x1920")])
                self.assertEqual(broker.size, (1080, 1920))

    def test_first_rejected_write_is_restored(self):
        broker = AndroidDisplay(fail=("size", "200x356"))
        with self.assertRaisesRegex(RuntimeError, "rejected display size"):
            apply_metrics(broker, self.config, self.backup)
        self.assertEqual(broker.writes, [("size", "200x356"), ("size", "1080x1920")])
        self.assertEqual((broker.size, broker.density), ((1080, 1920), 420))

    def test_invalid_physical_metadata_has_no_backup_or_writes(self):
        broker = AndroidDisplay(invalid=True)
        with self.assertRaises(ValueError):
            apply_metrics(broker, self.config, self.backup)
        self.assertEqual(broker.writes, [])
        self.assertFalse(self.backup.exists())

    def test_null_scale_does_not_touch_transport_or_backup(self):
        broker = Mock()
        result = apply_metrics(broker, validate_config({"scale": None}), self.backup)
        self.assertEqual(result, {"changed": False, "preserved": True})
        broker.owned_call.assert_not_called()
        self.assertFalse(self.backup.exists())


class ResourceBootIdentityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = validate_config({})
        self.owned = {"Id": "a" * 64, "State": {"Running": True, "StartedAt": "boot-1"}}
        self.manager = Mock()
        self.manager._owned_container_record.return_value = (self.owned, None)
        self.broker = Mock()
        self.broker.set_frame_rate.return_value = {"changed": False, "proxy": {"ready": True}}
        self.broker_factory = Mock(return_value=self.broker)
        fake_host = types.ModuleType("script_host")
        fake_host.Broker = self.broker_factory
        for context in (
                patch.dict(GLOBALS, {"STATE": self.root, "native_manager": lambda _: self.manager}),
                patch.dict("sys.modules", {"script_host": fake_host}),
                patch.dict(GLOBALS, {"apply_metrics": Mock(return_value={"changed": False})})):
            context.start()
            self.addCleanup(context.stop)

    def write_marker(self, **changes):
        key = {"container_id": self.owned["Id"], "started_at": "boot-1",
               "config_sha256": hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest(),
               **changes}
        (self.root / "phone-1.json").write_text(json.dumps({"key": key}))

    def test_same_boot_and_config_leave_later_script_settings_alone(self):
        self.write_marker()
        result = reconcile("phone-1", self.config)
        self.assertEqual(result["skipped"], "already_applied")
        self.broker_factory.assert_not_called()

    def test_timer_rereads_disable_under_device_lock_before_applying(self):
        apply = Mock()
        values = [self.config, {**self.config, "enabled": False}]
        with patch.dict(GLOBALS, {"load_config": Mock(side_effect=values), "reconcile": apply}), \
                patch("os.geteuid", return_value=0), patch("builtins.print"):
            self.assertEqual(GLOBALS["main"](["--device", "phone-1"]), 0)
        apply.assert_not_called()

    def test_disabled_timer_fast_path_does_not_lock_or_apply(self):
        apply = Mock()
        disabled = {**self.config, "enabled": False}
        with patch.dict(GLOBALS, {"load_config": Mock(return_value=disabled), "reconcile": apply}), \
                patch("os.geteuid", return_value=0), patch("builtins.print"), patch("fcntl.flock") as lock:
            self.assertEqual(GLOBALS["main"](["--device", "phone-1"]), 0)
        apply.assert_not_called()
        lock.assert_not_called()

    def test_disabled_force_waits_for_started_timer_then_reports_without_apply(self):
        self.assert_force_waits_for_started_timer(enabled=False)

    def test_enabled_force_waits_for_started_timer_then_converges(self):
        self.assert_force_waits_for_started_timer(enabled=True)

    def assert_force_waits_for_started_timer(self, *, enabled):
        # The older timer started while enabled and owns the per-device lock.
        # Off+Apply must wait for that operation to finish before panel rollback.
        profile = {**self.config, "enabled": enabled}
        waiting = threading.Event()
        completed = threading.Event()
        real_flock = fcntl.flock
        apply = Mock(return_value={"ok": True})
        results = []
        errors = []
        with open(self.root / "phone-1.lock", "a") as old_timer:
            real_flock(old_timer, fcntl.LOCK_EX)
            def observed_flock(descriptor, operation):
                if operation == fcntl.LOCK_EX:
                    waiting.set()
                return real_flock(descriptor, operation)
            def off_and_apply():
                try:
                    results.append(GLOBALS["main"](["--device", "phone-1", "--force"]))
                except BaseException as error:
                    errors.append(error)
                finally:
                    completed.set()
            with patch.dict(GLOBALS, {"load_config": Mock(return_value=profile), "reconcile": apply}), \
                    patch("os.geteuid", return_value=0), patch("builtins.print"), \
                    patch("fcntl.flock", side_effect=observed_flock):
                worker = threading.Thread(target=off_and_apply, daemon=True)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(2), "Explicit disabled apply did not take the resource lock")
                    self.assertFalse(completed.is_set(), "Disabled apply skipped an in-flight timer")
                finally:
                    real_flock(old_timer, fcntl.LOCK_UN)
                    worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [0])
        if enabled:
            apply.assert_called_once_with("phone-1", profile, force=True)
        else:
            apply.assert_not_called()

    def test_same_container_new_boot_reapplies(self):
        self.write_marker()
        self.owned["State"]["StartedAt"] = "boot-2"
        result = reconcile("phone-1", self.config)
        self.assertTrue(result["ok"])
        self.broker.set_frame_rate.assert_called_once_with(20, timeout=180)
        marker = json.loads((self.root / "phone-1.json").read_text())
        self.assertEqual(marker["key"]["started_at"], "boot-2")

    def test_changed_configuration_reapplies_without_container_recreation(self):
        self.write_marker()
        config = validate_config({"fps": 30})
        result = reconcile("phone-1", config)
        self.assertTrue(result["ok"])
        self.broker.set_frame_rate.assert_called_once_with(30, timeout=180)
        self.assertEqual(result["key"]["container_id"], self.owned["Id"])

    def test_stopped_container_is_not_started(self):
        self.owned["State"]["Running"] = False
        self.assertEqual(reconcile("phone-1", self.config)["skipped"], "stopped")
        self.broker_factory.assert_not_called()
        self.manager.start.assert_not_called()

    def test_failed_proxy_recovery_is_not_marked_complete(self):
        self.broker.set_frame_rate.return_value = {"changed": False, "proxy": {"ready": False}}
        result = reconcile("phone-1", self.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "proxy_recovery_pending")
        marker = json.loads((self.root / "phone-1.json").read_text())
        self.assertFalse(marker["ok"])
        self.assertTrue(marker["proxy_pending"])

    def test_pending_proxy_retry_does_not_reapply_display(self):
        self.broker.set_frame_rate.return_value = {"changed": False, "proxy": {"ready": False}}
        reconcile("phone-1", self.config)
        self.broker.reset_mock()
        GLOBALS["apply_metrics"].reset_mock()
        self.broker.owned_manager.return_value = contextlib.nullcontext(self.manager)
        self.broker.display_proxy_reconcile.return_value = {"ready": True, "reconciled": True}
        result = reconcile("phone-1", self.config)
        self.assertTrue(result["ok"])
        self.assertTrue(result["proxy_retry_only"])
        self.assertFalse(result["proxy_pending"])
        self.broker.set_frame_rate.assert_not_called()
        GLOBALS["apply_metrics"].assert_not_called()
        self.broker.display_proxy_reconcile.assert_called_once()
        marker = json.loads((self.root / "phone-1.json").read_text())
        self.assertTrue(marker["ok"])
        self.assertFalse(marker["proxy_pending"])

    def test_unrecovered_proxy_remains_pending_without_display_writes(self):
        self.broker.set_frame_rate.return_value = {"changed": False, "proxy": {"ready": False}}
        reconcile("phone-1", self.config)
        self.broker.reset_mock()
        GLOBALS["apply_metrics"].reset_mock()
        self.broker.owned_manager.return_value = contextlib.nullcontext(self.manager)
        self.broker.display_proxy_reconcile.return_value = {"ready": False}
        result = reconcile("phone-1", self.config)
        self.assertFalse(result["ok"])
        self.assertTrue(result["proxy_pending"])
        self.broker.set_frame_rate.assert_not_called()
        GLOBALS["apply_metrics"].assert_not_called()

    def test_rejected_display_write_is_not_marked_complete(self):
        with patch.dict(GLOBALS, {"apply_metrics": Mock(side_effect=RuntimeError("rejected"))}):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                reconcile("phone-1", self.config)
        self.assertFalse((self.root / "phone-1.json").exists())


if __name__ == "__main__":
    unittest.main()
