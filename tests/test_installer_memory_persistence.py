"""Installer headroom migration contracts; no host/guest command is launched."""
import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qa"))
spec = importlib.util.spec_from_file_location("installer_memory_persistence", ROOT / "qa/qa_memory_persistence.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class InstallerMemoryPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"enabled": True, "render_fps": 15, "memory_reserve_mib": 256}
        self.owned = {"Id": "a" * 64, "State": {"Running": True}}
        self.depth = 0
        @contextlib.contextmanager
        def lock(**kwargs):
            self.assertEqual(kwargs, {"timeout_seconds": 60})
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
        self.manager = types.SimpleNamespace(
            context=types.SimpleNamespace(state_root=Path(self.temp.name)),
            _owned_container_record=Mock(side_effect=lambda **kwargs: (self.owned, None)),
            instance_operation_lock=Mock(side_effect=lock))
        self.reply = {"ok": True, "hardCapsChanged": False, "afterHigh": "1879048192", "changed": False,
                      "systemd": {"backend": "systemd", "memoryHigh": "1879048192"}}
        self.real_ensure = m.resources.ensure_memory_headroom
        def ensure(*args, **kwargs):
            self.assertEqual(self.depth, 1, "Setter and readback must share the official native owner")
            self.assertIs(args[0], self.manager)
            self.assertEqual(args[1], "a" * 64)
            return copy.deepcopy(self.reply)
        for name, value in (("native_manager", Mock(return_value=self.manager)),
                            ("load_config", Mock(side_effect=lambda: dict(self.config))),
                            ("ensure_memory_headroom", Mock(side_effect=ensure)),
                            ("registry_names", Mock(return_value=["phone-1"]))):
            replace = patch.object(m.resources, name, value)
            replace.start()
            self.addCleanup(replace.stop)

    def test_apply_uses_only_memory_owner_and_refreshed_policy(self):
        self.config["memory_reserve_mib"] = 128
        self.reply.update(afterHigh="2013265920", systemd={"backend": "systemd", "memoryHigh": "2013265920"})
        result = m.persist_one("phone-1", {"memory_reserve_mib": 256})
        self.assertTrue(result["ok"])
        m.resources.ensure_memory_headroom.assert_called_once_with(
            self.manager, "a" * 64, reserve_mib=128, operation="apply")
        self.assertEqual(self.depth, 0)

    def test_verify_requires_both_kernel_and_systemd_policy_values(self):
        for key in ("raw", "unit"):
            with self.subTest(key=key):
                if key == "raw":
                    self.reply["afterHigh"] = "max"
                else:
                    self.reply["afterHigh"] = "1879048192"
                    self.reply["systemd"]["memoryHigh"] = "infinity"
                with self.assertRaisesRegex(RuntimeError, "policy_not_satisfied"):
                    m.persist_one("phone-1", self.config, verify=True)
        self.assertTrue(all(call.kwargs["operation"] == "inspect"
                            for call in m.resources.ensure_memory_headroom.call_args_list))

    def test_stopped_private_device_never_locked_or_changed(self):
        self.owned["State"]["Running"] = False
        result = m.persist_one("phone-6", self.config)
        self.assertEqual(result["skipped"], "stopped")
        self.manager.instance_operation_lock.assert_not_called()
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_absent_unavailable_or_foreign_native_lookup_never_claims_skip(self):
        for reason in ("instance container does not exist", "foreign owner", None):
            for stage in ("before_lock", "inside_lock"):
                with self.subTest(reason=reason, stage=stage):
                    self.manager._owned_container_record.side_effect = (
                        [(None, reason)] if stage == "before_lock" else [(self.owned, None), (None, reason)])
                    with self.assertRaisesRegex(RuntimeError, "owned_runtime_unproven"):
                        m.persist_one("phone-1", self.config)
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_unknown_running_state_is_not_a_stopped_device(self):
        for state in (None, "false", 0, 1):
            for stage in ("before_lock", "inside_lock"):
                with self.subTest(state=state, stage=stage):
                    invalid = {"Id": "a" * 64, "State": {"Running": state}}
                    self.manager._owned_container_record.side_effect = (
                        [(invalid, None)] if stage == "before_lock" else [(self.owned, None), (invalid, None)])
                    with self.assertRaisesRegex(RuntimeError, "runtime_state_unproven"):
                        m.persist_one("phone-1", self.config)
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_device_that_stops_while_waiting_never_changed(self):
        self.manager._owned_container_record.side_effect = [(self.owned, None),
            ({"Id": "b" * 64, "State": {"Running": False}}, None)]
        self.assertEqual(m.persist_one("phone-1", self.config)["skipped"], "runtime_stopped_while_waiting")
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_policy_disabled_while_waiting_prevents_stale_apply(self):
        self.config["enabled"] = False
        self.assertEqual(m.persist_one("phone-1", {"enabled": True})["skipped"], "memory_policy_disabled")
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_pending_native_transaction_is_retained_without_setter(self):
        journal = Path(self.temp.name) / "device-regenerate-v3.json"
        original = b'{"phase":"storage_identity_committed"}'
        journal.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "native_transaction_pending"):
            m.persist_one("phone-1", self.config)
        self.assertEqual(journal.read_bytes(), original)
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_lock_busy_is_not_terminated_or_skipped_as_success(self):
        self.manager.instance_operation_lock.side_effect = TimeoutError("ordinary-owner-still-running")
        with patch.object(m.os, "geteuid", return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(["--apply"]), 1)
        self.assertIn('"ok": false', output.getvalue())
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_default_is_inert_and_empty_new_host_succeeds(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main([]), 0)
        m.resources.load_config.assert_not_called()
        m.resources.registry_names.return_value = []
        with patch.object(m.os, "geteuid", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["--apply"]), 0)
        m.resources.ensure_memory_headroom.assert_not_called()

    def test_disabled_policy_never_enumerates_instances(self):
        self.config["enabled"] = False
        with patch.object(m.os, "geteuid", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["--apply"]), 0)
        m.resources.registry_names.assert_not_called()

    def test_zero_reserve_verifies_original_infinity_and_max(self):
        self.config["memory_reserve_mib"] = 0
        self.reply.update(afterHigh="max", systemd={"backend": "systemd", "memoryHigh": "infinity"})
        self.assertTrue(m.persist_one("phone-1", self.config, verify=True)["ok"])

    def test_actual_zero_reserve_unowned_contract_skips_without_false_verification(self):
        self.config["memory_reserve_mib"] = 0
        m.resources.ensure_memory_headroom.side_effect = self.real_ensure
        original = ("a" * 64, "sha256:" + "b" * 64, 123, "started", "456")
        with patch.object(m.resources, "STATE", Path(self.temp.name) / "qa-state"), \
                patch.object(m.resources, "_memory_binding", return_value=(original, "/owned")) as binding, \
                patch.object(m.resources, "_memory_scope_read") as scope, \
                patch.object(m.resources, "_memory_write") as setter:
            for verify in (False, True):
                result = m.persist_one("phone-1", self.config, verify=verify)
                self.assertEqual(result["skipped"], "disabled_no_owned_receipt")
                self.assertFalse(result["unit_or_kernel_policy_verified"])
            self.assertEqual(binding.call_count, 4)
            self.assertTrue(all(call.kwargs == {"qualified": False} for call in binding.call_args_list))
            scope.assert_not_called()
            setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
