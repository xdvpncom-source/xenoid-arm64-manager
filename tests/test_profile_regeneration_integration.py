"""The profile phase must not rerun native GEN on a partial completion."""
from __future__ import annotations
import copy
import ast
from collections.abc import Mapping
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock

import test_regeneration_sdk as regen
from qa_profile_runtime import ProfilePending
h = regen.h


class ProfileRegenerationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = regen.RegenerationRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.broker, self.manager = self.fixture.broker, self.fixture.manager
        self.broker.capture_regeneration_profile = Mock(return_value={"enabled": True, "pinned": 1})
        self.broker.regeneration_native_completion = Mock(side_effect=lambda manager, cli, state, result, deadline: {"result": copy.deepcopy(result), "stable_digest": state["target"]["stableDigest"]})
        self.broker.verify_regeneration_native_completion = Mock(side_effect=lambda manager, cli, receipt, deadline: copy.deepcopy(receipt["native_completion"]["result"]))
        self.broker.complete_regeneration_profile = Mock(return_value={"ok": True, "official_apply": True})

    def test_profile_plan_is_pinned_before_native_and_checkpointed_without_google_plan(self):
        cli = regen.native_cli(self.manager)
        command = cli.cmd_device_regenerate
        def run(args):
            receipt = self.broker.regeneration_receipts.load("profile")
            self.assertEqual(receipt["profile_plan"], {"enabled": True, "pinned": 1})
            return command(args)
        cli.cmd_device_regenerate = run
        self.broker.regeneration_cli = lambda: cli
        result = self.broker.regenerate(0, "profile")
        self.assertTrue(result["profile_apply"]["official_apply"])
        self.broker.regeneration_native_completion.assert_called_once()
        self.assertEqual(self.manager.rotations, 1)

    def test_profile_pending_retry_does_not_call_gen_again_without_google_plan(self):
        self.broker.complete_regeneration_profile.side_effect = [h.RegenerationPending("profile_reload_pending"), {"ok": True}]
        with self.assertRaises(h.RegenerationPending): self.broker.regenerate(0, "profile")
        receipt = self.broker.regeneration_receipts.load("profile")
        self.assertIsNotNone(receipt.get("native_completion"))
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(self.manager.rotations, 1)
        result = self.broker.regenerate(0, "profile")
        self.assertTrue(result["profile_apply"]["ok"])
        self.assertEqual(self.manager.rotations, 1)
        self.assertEqual(self.manager.commands, 1)
        self.broker.capture_regeneration_profile.assert_called_once()

    def test_capture_failure_prevents_native_identity_rotation(self):
        self.broker.capture_regeneration_profile.side_effect = ProfilePending("profile_policy_invalid")
        with self.assertRaises(h.RegenerationPending) as error: self.broker.regenerate(0, "profile")
        self.assertEqual(error.exception.code, "profile_policy_invalid")
        self.assertEqual(self.manager.rotations, 0)
        self.assertEqual(self.manager.commands, 0)

    def test_real_oversized_policy_preflight_prevents_native_gen(self):
        import json
        import qa_profile_runtime as runtime
        from test_qa_profile_runtime import RuntimeTest
        preflight = RuntimeTest(); preflight.setUp(); self.addCleanup(preflight.doCleanups)
        fingerprint = preflight.base["build"]["fingerprint"]
        preflight.policy["fields"]["build.fingerprint"]["values"] = [fingerprint + "x" * (80-len(fingerprint))]
        preflight.policy_file.write_text(json.dumps(preflight.policy))
        self.broker.capture_regeneration_profile.side_effect = lambda *_args: runtime.capture_plan(
            preflight.broker, preflight.manager, preflight.manager.binding, None,
            config_path=preflight.policy_file, resource_path=preflight.resource_file)
        with self.assertRaises(h.RegenerationPending) as raised: self.broker.regenerate(0, "oversized-profile")
        self.assertEqual(raised.exception.code, "profile_property_capacity_exceeded")
        self.assertEqual(self.manager.rotations, 0); self.assertEqual(self.manager.commands, 0)

    def test_existing_pending_receipt_does_not_adopt_new_profile_policy(self):
        self.broker.capture_regeneration_profile.return_value = None
        self.manager.failure = "prepared"
        with self.assertRaises(h.RegenerationPending): self.broker.regenerate(0, "profile")
        self.broker.capture_regeneration_profile.return_value = {"enabled": True, "new": True}
        self.broker.complete_regeneration_profile.return_value = None
        self.broker.regenerate(0, "profile")
        receipt = self.broker.regeneration_receipts.load("profile")
        self.assertNotIn("profile_plan", receipt)
        self.broker.capture_regeneration_profile.assert_called_once()

    def test_cached_keybox_failure_resumes_durable_preparation_without_reroll(self):
        # Exercise the production owner method without importing a native SDK
        # checkout into this manager-only suite. The separate native contract
        # uses the real DaemonClient and complete bootstrap sequence.
        source = Path(__file__).resolve().parents[1] / 'nativebundle/source/src/xenoid/backend.py'
        owner = next(n for n in ast.parse(source.read_text()).body
                     if isinstance(n, ast.ClassDef) and n.name == 'RuntimeManager')
        methods = [n for n in owner.body if isinstance(n, ast.FunctionDef)
                   and n.name in {'reconcile_keybox', '_convergence_action_name'}]
        namespace = {'Any': Any, 'Mapping': Mapping}
        tree = ast.Module(body=[ast.ClassDef(name='Owner', bases=[], keywords=[],
                          body=methods, decorator_list=[])], type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), namespace)
        self.manager.reconcile_keybox = MethodType(namespace['Owner'].reconcile_keybox, self.manager)
        self.manager._convergence_action_name = namespace['Owner']._convergence_action_name
        peer = SimpleNamespace(native_available=False, cached_ready=False, attempts=0)
        self.manager.daemon_client = lambda **kw: SimpleNamespace(keybox_status=lambda **kw:
            {'ok': True, 'ready': True, 'configured': False} if peer.cached_ready else
            {'ok': False, 'error': 'clear_failed'})
        def bootstrap():
            self.assertTrue(self.manager.held)
            peer.attempts += 1
            peer.cached_ready = peer.native_available
            return {'ok': True, 'controlReady': True}
        self.manager.reconcile_bootstrap = bootstrap
        store = self.broker.regeneration_receipts
        cli = regen.cli_with_preparation(self.manager, store)
        base = cli.ConvergenceExecutor
        class KeyboxExecutor(base):
            def _phase_call(inner, phase, method, reporter, deadline, **kwargs):
                answer = inner.manager.reconcile_keybox('reconcile')
                if not answer['ok']:
                    raise regen.ConvergenceError(answer['error'], phase='keybox_converged')
                return answer
        cli.ConvergenceExecutor = KeyboxExecutor
        self.broker.regeneration_cli = lambda: cli
        with self.assertRaises(h.RegenerationPending) as raised:
            self.broker.regenerate(0, 'key')
        self.assertEqual(raised.exception.code, 'clear_failed')
        pending = store.load('key')
        self.assertEqual(pending['preparation']['status'], 'pending')
        self.assertNotIn('native_journal', pending)
        self.assertEqual(self.manager.rotations, 0)
        original_profile = copy.deepcopy(pending['profile_plan'])
        original_plan = pending['preparation']['plan_digest']
        self.broker.capture_regeneration_profile.return_value = {'different_admin_policy': True}
        self.broker.regeneration_receipts = h.RegenerationReceipts(self.fixture.root, regen.BASE['run_id'], 1)
        peer.native_available = True
        result = self.broker.regenerate(0, 'key')
        self.assertTrue(result['ok'])
        saved = self.broker.regeneration_receipts.load('key')
        self.assertEqual(saved['status'], 'committed')
        self.assertEqual(saved['profile_plan'], original_profile)
        self.assertEqual(saved['preparation']['plan_digest'], original_plan)
        self.assertEqual(self.manager.rotations, 1)
        self.assertEqual(self.manager.commands, 1)
        self.assertEqual(peer.attempts, 2)
        self.assertTrue(self.broker.regenerate(0, 'key')['replayed'])
        self.assertEqual(self.manager.rotations, 1)
        self.assertEqual(self.manager.rotations, 1)

    def test_shared_failure_wrapper_keeps_profile_error_code(self):
        with unittest.mock.patch("qa_profile_runtime.complete", side_effect=ProfilePending("profile_effective_json_mismatch")):
            with self.assertRaises(h.RegenerationPending) as raised:
                h.Broker.complete_regeneration_profile(self.broker, self.manager, object(), {"profile_plan": {}}, self.broker.regeneration_receipts, "x", None)
        self.assertEqual(raised.exception.code, "profile_effective_json_mismatch")


if __name__ == "__main__": unittest.main()
