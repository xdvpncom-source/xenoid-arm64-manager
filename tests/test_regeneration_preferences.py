"""Exercise preferences through the real SDK, GEN broker and recovery wiring."""
import contextlib
import copy
import json
import sys
import unittest
from unittest.mock import Mock, patch

import test_regeneration_sdk as regen
import qa_ui_preferences as ui
from test_cycle_google_cache import CacheDevice, m as cycle
from test_vpn_cycle import Clock

h, sdk = regen.h, regen.sdk
CONFIG = {"enabled": True, "regions": [ui.DEFAULT_REGIONS[0]],
          "font_scales": [0.85, 1.0], "night_modes": [1, 2]}
RESULT = {"ok": True, "verified": True, "target": {"locales": ["en-US"]}}


class PreferencesRegenerationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = regen.RegenerationRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.broker, self.manager = self.fixture.broker, self.fixture.manager
        self.broker.capture_regeneration_profile = Mock(return_value=None)
        self.broker.complete_regeneration_profile = Mock(return_value=None)
        self.broker.regeneration_native_completion = Mock(side_effect=
            lambda manager, cli, state, result, deadline: {
                "result": copy.deepcopy(result), "stable_digest": state["target"]["stableDigest"]})
        self.broker.verify_regeneration_native_completion = Mock(side_effect=
            lambda manager, cli, receipt, deadline: copy.deepcopy(receipt["native_completion"]["result"]))
        self.plan = {"enabled": True, "pinned": "single-selection"}
        self.capture_patch = patch.object(ui, "capture", return_value=copy.deepcopy(self.plan))
        self.capture = self.capture_patch.start(); self.addCleanup(self.capture_patch.stop)
        self.complete_patch = patch.object(ui, "complete", return_value=copy.deepcopy(RESULT))
        self.preferences = self.complete_patch.start(); self.addCleanup(self.complete_patch.stop)

    def invoke(self, key="preferences", config=CONFIG):
        return self.broker.regenerate(0, key, post_preferences=config)

    def test_sdk_fifth_argument_preserves_optional_and_legacy_wire_shapes(self):
        cache = {"packages": ["example.app"], "user": "current", "timeout": 0}
        google = {"clear_packages": ["com.google.android.gms"]}
        with patch.object(sdk, "request", side_effect=lambda action, args: (action, args)):
            self.assertEqual(sdk.Device().regenerate(0, "key", post_preferences=CONFIG),
                             ("regenerate", [0, "key", None, None, CONFIG]))
            self.assertEqual(sdk.Device().regenerate(0, "key", post_cache=cache,
                                                    post_google=google, post_preferences=CONFIG),
                             ("regenerate", [0, "key", cache, google, CONFIG]))
            self.assertEqual(sdk.Device().regenerate(0, "key"), ("regenerate", [0, "key"]))
            self.assertEqual(sdk.Device().regenerate(0, "key", post_cache=cache),
                             ("regenerate", [0, "key", cache]))
            self.assertEqual(sdk.Device().regenerate(0, "key", post_google=google),
                             ("regenerate", [0, "key", None, google]))
            self.assertEqual(sdk.Device().preferences_snapshot(), ("ui-preferences-read", []))
            self.assertEqual(sdk.Device().restore_preferences(), ("ui-preferences-restore", []))
            self.assertEqual(sdk.Device().preferences_baseline(), ("ui-preferences-baseline", []))

    def test_dispatch_accepts_exact_five_argument_request(self):
        with patch.object(self.broker, "regenerate", return_value="accepted") as call:
            args = [0, "key", None, None, CONFIG]
            self.assertEqual(self.broker.dispatch("regenerate", args), "accepted")
            call.assert_called_once_with(*args)

    def test_capture_and_plan_are_durable_before_first_native_command(self):
        cli = regen.native_cli(self.manager)
        original = cli.cmd_device_regenerate
        def command(args):
            saved = self.broker.regeneration_receipts.load("preferences")
            self.assertEqual(saved["post_preferences_plan"], self.plan)
            self.assertEqual(saved["post_preferences"], ui.normalize_plan(CONFIG))
            self.assertTrue(self.manager.held)
            return original(args)
        cli.cmd_device_regenerate = command
        self.broker.regeneration_cli = lambda: cli
        result = self.invoke()
        self.assertTrue(result["ui_preferences"]["verified"])
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.capture.call_args.args[3], ui.normalize_plan(CONFIG))
        self.assertEqual(self.capture.call_args.args[5], "preferences")
        self.assertEqual(self.manager.commands, 1)

    def test_capture_failure_prevents_native_generation(self):
        self.capture.side_effect = ui.PreferencesPending("ui_preferences_snapshot_invalid")
        with self.assertRaisesRegex(h.RegenerationPending, "ui_preferences_snapshot_invalid"):
            self.invoke()
        self.assertEqual(self.manager.commands, 0)
        self.assertEqual(self.manager.rotations, 0)

    def test_native_region_uses_the_captured_plan_before_native_reservation(self):
        options = {"location_country": "US", "location_timezone": "America/Chicago",
                   "location_locales": ["en-US"], "randomize_carrier": True}
        config = {**CONFIG, "native_location": {"country": "US", "randomize_carrier": True}}
        cli = regen.native_cli(self.manager)
        original = cli.cmd_device_regenerate
        def command(args):
            for key, value in options.items():
                self.assertEqual(getattr(args, key), value)
            self.assertEqual(self.broker.regeneration_receipts.load("preferences")["post_preferences_plan"], self.plan)
            return original(args)
        cli.cmd_device_regenerate = command
        self.broker.regeneration_cli = lambda: cli
        with patch.object(ui, "native_location_args", return_value=options) as derive:
            self.invoke(config=config)
        derive.assert_called_once_with(self.plan)

    def test_daemon_preparation_precedes_capture_and_capture_precedes_native_gen(self):
        order = []
        self.broker.prepare_regeneration = Mock(side_effect=lambda *_a, **_k: order.append("prepare"))
        self.capture.side_effect = lambda *_a, **_k: order.append("capture") or copy.deepcopy(self.plan)
        cli = regen.native_cli(self.manager)
        original = cli.cmd_device_regenerate
        def command(args):
            order.append("gen")
            self.assertEqual(self.broker.regeneration_receipts.load("preferences")["post_preferences_plan"], self.plan)
            return original(args)
        cli.cmd_device_regenerate = command
        self.broker.regeneration_cli = lambda: cli
        self.invoke()
        self.assertEqual(order, ["prepare", "capture", "gen"])

    def test_preparation_failure_never_calls_missing_old_daemon_helper(self):
        self.broker.prepare_regeneration = Mock(side_effect=h.RegenerationPending("native_preparation_failed"))
        with self.assertRaisesRegex(h.RegenerationPending, "native_preparation_failed"):
            self.invoke()
        self.capture.assert_not_called()
        self.assertEqual(self.manager.commands, 0)

    def test_capture_retry_pins_once_before_first_native_command(self):
        self.capture.side_effect = [ui.PreferencesPending("ui_preferences_timeout"), copy.deepcopy(self.plan)]
        with self.assertRaisesRegex(h.RegenerationPending, "ui_preferences_timeout"):
            self.invoke()
        self.assertNotIn("post_preferences_plan", self.broker.regeneration_receipts.load("preferences"))
        self.assertTrue(self.invoke()["ok"])
        self.assertTrue(self.invoke()["replayed"])
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.manager.commands, 1)

    def test_saved_native_checkpoint_without_preferences_plan_is_not_recaptured(self):
        self.invoke()
        saved = self.broker.regeneration_receipts.load("preferences")
        saved.pop("post_preferences_plan")
        saved["status"] = "pending"
        self.broker.regeneration_receipts.save("preferences", saved)
        with self.assertRaisesRegex(h.RegenerationPending, "finish_existing_regeneration_before_preferences"):
            self.invoke()
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.manager.commands, 1)

    def test_same_key_rejects_changed_preference_config_before_another_native_call(self):
        self.invoke()
        for changed in (None, {**CONFIG, "font_scales": [1.15]}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    h.RegenerationPending, "regeneration_post_preferences_plan_changed"):
                self.invoke(config=changed)
        self.assertEqual(self.manager.commands, 1)
        self.assertEqual(self.capture.call_count, 1)

    def test_pending_preferences_resume_pinned_target_without_native_rotation(self):
        self.preferences.side_effect = [ui.PreferencesPending("ui_preferences_rollback_pending"), copy.deepcopy(RESULT)]
        with self.assertRaisesRegex(h.RegenerationPending, "ui_preferences_rollback_pending"):
            self.invoke()
        saved = self.broker.regeneration_receipts.load("preferences")
        self.assertEqual(saved["status"], "pending")
        self.assertIsNotNone(saved["native_completion"])
        self.assertEqual(saved["last_preferences_error"], "ui_preferences_rollback_pending")
        self.assertTrue(self.invoke()["ui_preferences"]["verified"])
        self.assertEqual(self.manager.commands, 1)
        self.assertEqual(self.manager.rotations, 1)
        self.capture.assert_called_once()
        self.assertEqual(self.broker.regeneration_receipts.load("preferences")["post_preferences_plan"], self.plan)

    def test_both_fresh_and_recovery_order_is_profile_preferences_cache_google(self):
        calls = []
        self.broker.complete_regeneration_profile.side_effect = lambda *_args: calls.append("profile")
        self.preferences.side_effect = lambda *_args: calls.append("preferences") or copy.deepcopy(RESULT)
        self.broker.complete_regeneration_post_cache = Mock(side_effect=lambda *_args: calls.append("cache"))
        self.broker.complete_regeneration_post_google = Mock(side_effect=lambda *_args: calls.append("google"))
        google = {"clear_packages": ["com.google.android.gms"], "user": "current", "delay_seconds": 2, "timeout": 0}
        self.broker.regenerate(0, "ordered", post_google=google, post_preferences=CONFIG)
        self.assertEqual(calls, ["profile", "preferences", "cache", "google"])
        saved = self.broker.regeneration_receipts.load("ordered")
        saved["status"] = "pending"
        self.broker.regeneration_receipts.save("ordered", saved)
        calls.clear()
        self.broker.regenerate(0, "ordered", post_google=google, post_preferences=CONFIG)
        self.assertEqual(calls, ["profile", "preferences", "cache", "google"])
        self.assertEqual(self.manager.commands, 1)

    def test_committed_replay_does_not_repeat_preference_apply(self):
        self.invoke()
        self.assertTrue(self.invoke()["replayed"])
        self.capture.assert_called_once()
        self.preferences.assert_called_once()
        self.assertEqual(self.manager.rotations, 1)

    def test_legacy_receipt_missing_optional_keys_never_reads_or_applies_preferences(self):
        self.complete_patch.stop()
        self.assertTrue(self.broker.regenerate(0, "legacy")["ok"])
        self.capture.assert_not_called()
        saved = self.broker.regeneration_receipts.load("legacy")
        saved.pop("post_preferences", None)
        self.broker.regeneration_receipts.save("legacy", saved)
        with patch.object(ui, "_helper", side_effect=AssertionError("legacy must not read preferences")):
            self.assertTrue(self.broker.regenerate(0, "legacy")["replayed"])
        self.assertEqual(self.manager.rotations, 1)

    def test_supervisor_replays_saved_preferences_before_new_app_iteration(self):
        self.preferences.side_effect = [ui.PreferencesPending("ui_preferences_rollback_pending"), copy.deepcopy(RESULT)]
        with self.assertRaises(h.RegenerationPending):
            self.invoke()
        saved_config = self.broker.regeneration_receipts.load("preferences")["post_preferences"]
        original = h.Broker.regenerate
        observed = []
        def regenerate(broker, timeout, key, post_cache=None, post_google=None, post_preferences=None, **kwargs):
            observed.append(copy.deepcopy(post_preferences))
            return original(broker, timeout, key, post_cache, post_google, post_preferences, **kwargs)
        with patch.object(h, "trusted_device", return_value=self.manager), \
             patch.object(h.Broker, "regeneration_cli", side_effect=lambda: regen.native_cli(self.manager)), \
             patch.object(h.Broker, "display_raw_docker", return_value=contextlib.nullcontext()), \
             patch.object(h.Broker, "display_bootstrap"), \
             patch.object(h.Broker, "complete_regeneration_profile", return_value=None), \
             patch.object(h.Broker, "verify_regeneration_native_completion", side_effect=
                          lambda manager, cli, receipt, deadline: copy.deepcopy(receipt["native_completion"]["result"])), \
             patch.object(h.Broker, "regenerate", new=regenerate):
            self.assertTrue(h.resume_regeneration_receipts(self.fixture.root, 1, regen.BASE, 1, {}))
        self.assertEqual(observed, [saved_config])
        self.assertEqual(self.broker.regeneration_receipts.load("preferences")["status"], "committed")
        self.assertEqual(self.manager.commands, 1)
        self.capture.assert_called_once()

    def test_manual_read_and_restore_execute_inside_owned_native_lock(self):
        # Use the real owned_manager context and its actual instance lock.
        class InstanceError(RuntimeError):
            pass
        fake_config = type(sys)("xenoid.config")
        fake_config.InstanceError = InstanceError
        fake_xenoid = type(sys)("xenoid")
        fake_xenoid.config = fake_config
        seen = []
        def read(broker, manager, action, payload, deadline):
            self.assertTrue(manager.held)
            self.assertIs(manager, self.manager)
            seen.append(action)
            return {"actual": True}
        def restore(broker, manager, **kwargs):
            self.assertTrue(manager.held)
            self.assertIs(manager, self.manager)
            seen.append("restore")
            return {"ok": True, "restored": True}
        def baseline(broker, manager, **kwargs):
            self.assertTrue(manager.held)
            seen.append("baseline")
            return {"snapshot": {"actual": True}, "native_country": "SG"}
        with patch.dict(sys.modules, {"xenoid": fake_xenoid, "xenoid.config": fake_config}), \
             patch.object(ui, "_helper", side_effect=read), patch.object(ui, "restore", side_effect=restore), \
             patch.object(ui, "read_baseline", side_effect=baseline):
            self.assertEqual(self.broker.dispatch("ui-preferences-read", []), {"ok": True, "snapshot": {"actual": True}})
            self.assertTrue(self.broker.dispatch("ui-preferences-restore", [])["restored"])
            self.assertEqual(self.broker.dispatch("ui-preferences-baseline", [])["native_country"], "SG")
        self.assertEqual(seen, ["read", "restore", "baseline"])
        self.assertEqual(len(self.manager.requests), 3)
        self.assertFalse(self.manager.held)


class PreferencesScenarioIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = CacheDevice(self.clock)
        self.loop = cycle.Cycle(self.device, "qa.vpn")
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name, value in {"REGENERATE_ENABLED": True, "GOOGLE_DATA_RESET_ENABLED": False,
                            "GOOGLE_CACHE_PACKAGES": (), "UI_PREFERENCES": copy.deepcopy(CONFIG)}.items():
            self.stack.enter_context(patch.object(cycle, name, value))
        self.stack.enter_context(patch.object(cycle, "time", self.clock))
        self.stack.enter_context(patch.object(cycle, "configure_resources", return_value=None))
        self.stack.enter_context(patch.object(cycle.random, "randint", return_value=cycle.REGENERATE_TRIGGER))

    def test_scenario_forwards_preferences_and_retries_same_key_until_verified(self):
        original = self.device.regenerate
        requests = []
        def regenerate(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            answer = original(**kwargs)
            if len(requests) == 1:
                answer["ui_preferences"]["verified"] = False
            return answer
        self.device.regenerate = regenerate
        self.assertTrue(self.loop.regenerate_after_success())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["operation_key"], requests[1]["operation_key"])
        self.assertEqual(requests[0]["post_preferences"], CONFIG)
        self.assertEqual(len(self.device.native_rotation_keys), 1)
        self.assertTrue(any(row[0] == "log" and "regeneration_user_preferences_pending" in row[2] for row in self.device.trace))

    def test_disabled_scenario_does_not_send_preferences_option(self):
        original = self.device.regenerate
        requests = []
        def regenerate(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return original(**kwargs)
        self.device.regenerate = regenerate
        with patch.object(cycle, "UI_PREFERENCES", {"enabled": False}):
            self.assertTrue(self.loop.regenerate_after_success())
        self.assertNotIn("post_preferences", requests[0])


if __name__ == "__main__":
    unittest.main()
