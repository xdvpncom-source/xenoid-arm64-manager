"""Pinned user preference selection, readback and recovery without Android."""
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
import qa_ui_preferences as ui


BINDING = {"instance_id": "12345678-1234-4234-8234-123456789abc",
           "resource_tag": "a" * 12, "container_id": "b" * 64}
LEGACY_REGIONS = [
    {"locales": ["en-US"], "timezone": "America/New_York", "time_12_24": "12", "date_format": "MM/dd/yyyy"},
    {"locales": ["en-GB"], "timezone": "Europe/London", "time_12_24": "24", "date_format": "dd/MM/yyyy"},
]


def initial_snapshot():
    return {"schema": ui.SNAPSHOT_SCHEMA, "user_id": 0,
            "values": {"locales": ["en-SG"], "timezone": "Asia/Singapore", "font_scale": 1.0, "night_mode": 0},
            "settings": {"system": {"system_locales": "en-SG", "font_scale": None, "time_12_24": None, "date_format": None},
                         "global": {"auto_time_zone": "1", "xenoid_qa_ui_preferences_v1": None}},
            "properties": {"locale": "en-SG", "timezone": "Asia/Singapore"}}


class Store:
    def __init__(self):
        self.saved = []

    def save(self, key, receipt):
        self.saved.append((key, copy.deepcopy(receipt)))


class Broker:
    def __init__(self):
        self.binding = copy.deepcopy(BINDING)
        self.stopping = threading.Event()
        self.snapshot = initial_snapshot()
        self.commands = []
        self.actions = []
        self.fail_apply = False
        self.lose_apply_reply = False
        self.fail_restore = False
        self.bad_readback = False
        self.store = None
        self.had_durable_apply_intent = None
        self.native_country = "SG"
        self.native_baseline_calls = 0
        self.android_env = (b"ANDROID_DATA=/data\0ANDROID_ROOT=/system\0ANDROID_RUNTIME_ROOT=/apex/com.android.runtime\0"
                            b"BOOTCLASSPATH=/apex/runtime/core.jar:/system/framework/framework.jar\0"
                            b"DEX2OATBOOTCLASSPATH=/system/framework/framework.jar\0"
                            b"SYSTEMSERVERCLASSPATH=/system/framework/services.jar\0"
                            b"STANDALONE_SYSTEMSERVER_JARS=/apex/example/service.jar\0"
                            b"LD_PRELOAD=/untrusted.so\0HOST_SECRET=private\0IGNORED=\xff\0")

    def regeneration_binding(self, manager):
        return copy.deepcopy(self.binding)

    def native_location_baseline(self, manager):
        self.native_baseline_calls += 1
        return self.native_country

    def native_display_exec(self, manager, args, **options):
        self.commands.append(copy.deepcopy(args))
        assert options["check"] is False
        if args == ["pm", "path", "dev.xenoid.daemon"]:
            return types.SimpleNamespace(returncode=0, stdout=b"package:/system/priv-app/XenoidDaemon/base.apk\n")
        if args == ["pidof", "system_server"]:
            return types.SimpleNamespace(returncode=0, stdout=b"173\n")
        if args == ["cat", "/proc/173/environ"]:
            return types.SimpleNamespace(returncode=0, stdout=self.android_env)
        assert args[:2] == ["env", "-i"]
        assert "CLASSPATH=/system/priv-app/XenoidDaemon/base.apk" in args
        index = args.index(ui.HELPER)
        assert args[index - 2:index] == ["/system/bin/app_process", "/system/bin"]
        action = args[index + 1]
        value = json.loads(base64.b64decode(args[index + 2])) if len(args) == index + 3 else None
        self.actions.append((action, copy.deepcopy(value)))
        if action == "apply":
            if self.store is not None:
                last = self.store.saved[-1][1]
                self.had_durable_apply_intent = last["post_preferences_progress"]["status"] == "applying"
            self.snapshot["values"] = {field: value[field] for field in ("locales", "timezone", "font_scale", "night_mode")}
            self.snapshot["settings"]["system"] = {
                "system_locales": ",".join(value["locales"]), "font_scale": str(value["font_scale"]),
                "time_12_24": value["time_12_24"], "date_format": value["date_format"]}
            self.snapshot["settings"]["global"] = {"auto_time_zone": "0", "xenoid_qa_ui_preferences_v1": json.dumps(value)}
            self.snapshot["properties"] = {"locale": value["locales"][0], "timezone": value["timezone"]}
            if self.bad_readback:
                self.snapshot["values"]["font_scale"] = 1.75
            if self.fail_apply:
                self.snapshot["values"]["timezone"] = "UTC"
                return types.SimpleNamespace(returncode=1, stdout=b'{"ok":false,"error":"ui_preferences_setting_failed","rollback_attempted":true,"rollback_succeeded":false}')
            if self.lose_apply_reply:
                self.lose_apply_reply = False
                raise TimeoutError("reply lost")
        elif action == "restore":
            if self.fail_restore:
                raise TimeoutError("restore transport unavailable")
            self.snapshot = copy.deepcopy(value)
        elif action != "read":
            raise AssertionError("Unexpected helper action")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "snapshot": self.snapshot}).encode())


class UiPreferencesTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix=".ui-test-", dir=str(Path(__file__).resolve().parents[1]))
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.broker = Broker()
        self.manager = object()
        self.store = Store()
        self.broker.store = self.store
        self.config = {"enabled": True, "regions": LEGACY_REGIONS, "font_scales": [0.85, 1.0], "night_modes": [1, 2]}

    def capture(self, key="run:1:GEN"):
        return ui.capture(self.broker, self.manager, BINDING, self.config, None, key, state_dir=self.root)

    def receipt(self, key="run:1:GEN"):
        return {"binding": copy.deepcopy(BINDING), "post_preferences_plan": self.capture(key)}

    def complete(self, receipt, key="run:1:GEN"):
        return ui.complete(self.broker, self.manager, receipt, self.store, key, None, state_dir=self.root)

    def test_disabled_configuration_needs_no_helper_or_file(self):
        for config in (None, {"enabled": False}):
            self.assertIsNone(ui.capture(self.broker, self.manager, BINDING, config, None, "key", state_dir=self.root))
        self.assertEqual(self.broker.commands, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_default_pool_has_only_four_coherent_us_presets(self):
        normalized = ui.normalize_plan({"enabled": True})
        self.assertEqual(len(normalized["regions"]), 4)
        self.assertEqual(normalized["font_scales"], [0.85, 1.0, 1.15])
        self.assertEqual(normalized["night_modes"], [1, 2])
        self.assertEqual({row["timezone"] for row in normalized["regions"]}, set(ui.US_TIMEZONES))
        self.assertTrue(all(row["locales"] == ["en-US"] and row["time_12_24"] == "12"
                            and row["date_format"] == "MM/dd/yyyy" for row in normalized["regions"]))
        self.assertNotIn("native_location", normalized)

    def test_native_location_is_explicit_validated_and_preserved(self):
        native = {"country": "US", "randomize_carrier": True}
        normalized = ui.normalize_plan({"enabled": True, "native_location": native})
        self.assertEqual(normalized["native_location"], native)
        native["country"] = "GB"
        self.assertEqual(normalized["native_location"]["country"], "US")
        for bad in (None, {}, {"country": "us", "randomize_carrier": True},
                    {"country": "GB", "randomize_carrier": True},
                    {"country": "US", "randomize_carrier": 1},
                    {"country": "US", "randomize_carrier": True, "other": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ui.normalize_plan({"enabled": True, "native_location": bad})

    def test_native_country_requires_matching_ui_regions(self):
        for region in (LEGACY_REGIONS[1], {**LEGACY_REGIONS[0], "locales": ["de-DE"]},
                       {**LEGACY_REGIONS[0], "timezone": "Europe/London"}):
            with self.subTest(region=region), self.assertRaises(ValueError):
                ui.normalize_plan({"regions": [region], "native_location": {"country": "US", "randomize_carrier": True}})

    def test_native_args_use_persisted_target_and_do_not_change_java_schema(self):
        self.config = {"enabled": True, "native_location": {"country": "US", "randomize_carrier": True}}
        plan = self.capture()
        args = ui.native_location_args(plan)
        self.assertEqual(args, {"location_country": "US", "randomize_carrier": True,
                                "location_timezone": plan["target"]["timezone"], "location_locales": ["en-US"]})
        self.assertIn(args["location_timezone"], ui.US_TIMEZONES)
        self.assertFalse(plan["independent_sim_region"])
        self.assertNotIn("country", plan["target"])
        self.assertNotIn("randomize_carrier", plan["target"])
        args["location_locales"].append("es-US")
        self.assertEqual(plan["target"]["locales"], ["en-US"])
        result = self.complete({"binding": copy.deepcopy(BINDING), "post_preferences_plan": plan})
        self.assertFalse(result["independent_sim_region"])
        plan["target"]["timezone"] = "Europe/London"
        with self.assertRaises(ui.PreferencesPending):
            ui.native_location_args(plan)

    def test_legacy_world_plans_do_not_implicitly_request_native_location(self):
        plan = self.capture()
        self.assertEqual(ui.native_location_args(None), {})
        self.assertEqual(ui.native_location_args(plan), {})
        self.assertNotIn("native_location", plan["config"])
        self.assertIn(LEGACY_REGIONS[1], plan["config"]["regions"])
        self.assertTrue(plan["independent_sim_region"])
        self.assertEqual(self.broker.native_baseline_calls, 0)

    def test_explicit_other_country_with_regions_supports_administrative_rollback(self):
        self.config = {"regions": [LEGACY_REGIONS[1]], "native_location": {"country": "GB", "randomize_carrier": False}}
        plan = self.capture()
        self.assertEqual(ui.native_location_args(plan), {"location_country": "GB", "location_timezone": "Europe/London",
                                                        "location_locales": ["en-GB"], "randomize_carrier": False})
        self.assertFalse(plan["independent_sim_region"])

    def test_native_attachment_preserves_original_ui_baseline_and_country_once(self):
        self.capture("legacy-ui")
        path = self.root / (BINDING["instance_id"] + ".baseline.json")
        original = path.read_bytes()
        self.config = {"native_location": {"country": "US", "randomize_carrier": True}}
        self.capture("first-us")
        native_path = self.root / (BINDING["instance_id"] + ".native-location-baseline.json")
        native_bytes = native_path.read_bytes()
        self.assertEqual(native_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), original)
        self.broker.native_country = "US"
        self.capture("later-us")
        self.assertEqual(native_path.read_bytes(), native_bytes)
        self.assertEqual(self.broker.native_baseline_calls, 1)
        before = len(self.broker.actions)
        baseline = ui.read_baseline(self.broker, self.manager, state_dir=self.root)
        self.assertEqual(set(baseline), {"snapshot", "native_country"})
        self.assertEqual(baseline["snapshot"], initial_snapshot())
        self.assertEqual(baseline["native_country"], "SG")
        self.assertEqual(len(self.broker.actions), before)

    def test_legacy_baseline_does_not_invent_native_country(self):
        self.capture()
        baseline = ui.read_baseline(self.broker, self.manager, state_dir=self.root)
        self.assertIsNone(baseline["native_country"])
        self.assertEqual(self.broker.native_baseline_calls, 0)

    def test_missing_baseline_is_reported_without_reading_android(self):
        with self.assertRaisesRegex(ui.PreferencesPending, "baseline_missing"):
            ui.read_baseline(self.broker, self.manager, state_dir=self.root)
        self.assertEqual(self.broker.actions, [])

    def test_unavailable_native_country_blocks_capture_before_any_preference_write(self):
        self.config = {"native_location": {"country": "US", "randomize_carrier": True}}
        self.broker.native_country = None
        with self.assertRaisesRegex(ui.PreferencesPending, "native_baseline_unavailable"):
            self.capture()
        self.assertEqual([action for action, _ in self.broker.actions], ["read"])
        self.assertFalse((self.root / (BINDING["instance_id"] + ".native-location-baseline.json")).exists())

    def test_native_country_tampering_and_flag_mismatch_are_rejected(self):
        self.config = {"native_location": {"country": "US", "randomize_carrier": True}}
        receipt = self.receipt()
        receipt["post_preferences_plan"]["independent_sim_region"] = True
        with self.assertRaisesRegex(ui.PreferencesPending, "plan_invalid"):
            self.complete(receipt)
        path = self.root / (BINDING["instance_id"] + ".native-location-baseline.json")
        value = json.loads(path.read_text())
        value["country"] = "US"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ui.PreferencesPending, "native_baseline_binding_or_digest_changed"):
            ui.read_baseline(self.broker, self.manager, state_dir=self.root)

    def test_invalid_configuration_rejected_before_device_access(self):
        invalid = [True, {"unexpected": True}, {"enabled": "yes"}, {"regions": []},
                   {"font_scales": [float("nan")]}, {"font_scales": [float("inf")]},
                   {"font_scales": [True]}, {"font_scales": [0]}, {"font_scales": [2.01]},
                   {"night_modes": [0]}, {"night_modes": [True]}, {"night_modes": []}]
        for change in ({"locales": ["en_US"]}, {"locales": ["en-US", "en-us"]},
                       {"timezone": "../etc/passwd"},
                       {"date_format": "anything"}, {"time_12_24": 12}):
            invalid.append({"regions": [{**ui.DEFAULT_REGIONS[0], **change}]})
        if ui.ZoneInfo is not None:
            invalid.append({"regions": [{**ui.DEFAULT_REGIONS[0], "timezone": "Europe/DoesNotExist"}]})
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                ui.capture(self.broker, self.manager, BINDING, config, None, "key", state_dir=self.root)
        self.assertEqual(self.broker.commands, [])

    def test_python38_timezone_fallback_still_rejects_traversal(self):
        with patch.object(ui, "ZoneInfo", None):
            self.assertEqual(ui._zone("Asia/Singapore"), "Asia/Singapore")
            for zone in ("../UTC", "/etc/localtime", "Asia//Singapore", "Etc/../UTC", "Asia/Singapore;id"):
                with self.assertRaises(ValueError):
                    ui._zone(zone)

    def test_capture_is_read_only_for_android_and_baseline_is_first_snapshot(self):
        first = self.capture()
        baseline = self.root / (BINDING["instance_id"] + ".baseline.json")
        old_bytes = baseline.read_bytes()
        self.assertEqual(baseline.stat().st_mode & 0o777, 0o600)
        self.assertEqual([action for action, _ in self.broker.actions], ["read"])
        second = self.capture()
        self.assertEqual(first["target"], second["target"])
        self.assertEqual(first["before_snapshot"], second["before_snapshot"])
        self.broker.snapshot["values"]["night_mode"] = 1
        self.capture("different:operation")
        self.assertEqual(baseline.read_bytes(), old_bytes)

    def test_next_capture_avoids_exact_previous_tuple(self):
        receipt = self.receipt()
        self.complete(receipt)
        next_plan = self.capture("next:operation")
        before = ui._observed_fields(self.broker.snapshot)
        self.assertNotEqual({field: next_plan["target"][field] for field in ui._FIELDS}, before)

    def test_new_selection_excludes_same_float32_font_setting(self):
        self.config = {"enabled": True, "regions": [ui.DEFAULT_REGIONS[0]], "font_scales": [0.85], "night_modes": [1, 2]}
        snapshot = self.broker.snapshot
        snapshot["values"].update(locales=["en-US"], timezone="America/New_York", font_scale=0.8500000238418579, night_mode=1)
        snapshot["settings"]["system"].update(time_12_24="12", date_format="MM/dd/yyyy")
        for index in range(20):
            plan = self.capture("float32-" + str(index))
            self.assertEqual(plan["selection_version"], 2)
            self.assertEqual(plan["target"]["night_mode"], 2)

    def test_pre_versioned_plan_retains_exact_old_seed_and_target(self):
        key = "legacy-fixed-key"
        plan = self.capture(key)
        plan.pop("selection_version")
        legacy = {"schema": ui.SCHEMA, "locales": ["en-GB"], "timezone": "Europe/London",
                  "time_12_24": "24", "date_format": "dd/MM/yyyy", "font_scale": 0.85,
                  "night_mode": 1, "operation_key": key}
        plan["target"], plan["target_sha256"] = legacy, ui._digest(legacy)
        receipt = {"binding": copy.deepcopy(BINDING), "post_preferences_plan": plan}
        self.assertEqual(ui._plan(receipt, key)["target"], legacy)
        self.assertTrue(self.complete(receipt, key)["verified"])

    def test_unknown_selection_version_cannot_apply(self):
        receipt = self.receipt()
        receipt["post_preferences_plan"]["selection_version"] = 3
        before = len(self.broker.commands)
        with self.assertRaisesRegex(ui.PreferencesPending, "selection_version_invalid"):
            self.complete(receipt)
        self.assertEqual(len(self.broker.commands), before)

    def test_interrupted_first_baseline_publication_leaves_no_partial_baseline(self):
        with patch.object(ui.os, "link", side_effect=OSError("publication interrupted")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual([action for action, _ in self.broker.actions], ["read"])
        self.capture()
        self.assertTrue((self.root / (BINDING["instance_id"] + ".baseline.json")).exists())

    def test_complete_reuses_persisted_target_and_verifies_separate_read(self):
        receipt = self.receipt()
        target = copy.deepcopy(receipt["post_preferences_plan"]["target"])
        result = self.complete(receipt)
        self.assertTrue(result["verified"])
        self.assertEqual(result["target"], target)
        self.assertTrue(self.broker.had_durable_apply_intent)
        self.assertEqual([action for action, _ in self.broker.actions], ["read", "read", "apply", "read"])
        self.complete(receipt)
        self.assertEqual([action for action, _ in self.broker.actions].count("apply"), 1)
        self.assertEqual(receipt["post_preferences_plan"]["target"], target)

    def test_receipt_tamper_or_operation_key_mismatch_stops_before_helper(self):
        for mutation in (lambda r: r["post_preferences_plan"]["target"].update(font_scale=1.9),
                         lambda r: r["post_preferences_plan"].update(operation_key="other")):
            receipt = self.receipt()
            before = len(self.broker.commands)
            mutation(receipt)
            with self.assertRaises(ui.PreferencesPending):
                self.complete(receipt)
            self.assertEqual(len(self.broker.commands), before)

    def test_live_binding_change_prevents_any_apply(self):
        receipt = self.receipt()
        self.broker.binding["container_id"] = "c" * 64
        before = len(self.broker.commands)
        with self.assertRaisesRegex(ui.PreferencesPending, "binding_changed"):
            self.complete(receipt)
        self.assertEqual(len(self.broker.commands), before)

    def test_partial_apply_restores_exact_pre_gen_snapshot_including_null_settings(self):
        receipt = self.receipt()
        original = copy.deepcopy(self.broker.snapshot)
        self.broker.fail_apply = True
        with self.assertRaisesRegex(ui.PreferencesPending, "failed_rolled_back"):
            self.complete(receipt)
        self.assertEqual(self.broker.snapshot, original)
        self.assertEqual(receipt["post_preferences_progress"]["status"], "rolled_back")
        self.assertIsNone(self.broker.snapshot["settings"]["system"]["font_scale"])

    def test_readback_failure_is_not_success_and_restores(self):
        receipt = self.receipt()
        self.broker.bad_readback = True
        with self.assertRaisesRegex(ui.PreferencesPending, "failed_rolled_back"):
            self.complete(receipt)
        self.assertEqual(self.broker.snapshot, initial_snapshot())

    def test_exact_readback_includes_marker_property_and_clock(self):
        receipt = self.receipt()
        self.complete(receipt)
        target = receipt["post_preferences_plan"]["target"]
        correct = copy.deepcopy(self.broker.snapshot)
        self.assertTrue(ui._matches(correct, target))
        for group, key, value in (("properties", "locale", "en-SG"),
                                  ("properties", "timezone", "UTC"),
                                  ("system", "time_12_24", None),
                                  ("system", "date_format", "yyyy"),
                                  ("global", "xenoid_qa_ui_preferences_v1", None),
                                  ("global", "auto_time_zone", "1")):
            changed = copy.deepcopy(correct)
            (changed[group] if group == "properties" else changed["settings"][group])[key] = value
            self.assertFalse(ui._matches(changed, target), (group, key))

    def test_single_available_tuple_may_remain_the_same(self):
        self.config = {"enabled": True, "regions": [ui.DEFAULT_REGIONS[0]], "font_scales": [1.0], "night_modes": [1]}
        receipt = self.receipt()
        self.complete(receipt)
        again = self.capture("second:key")
        self.assertEqual({field: again["target"][field] for field in ui._FIELDS}, ui._observed_fields(self.broker.snapshot))

    def test_plan_without_enabled_preference_stage_is_backward_compatible(self):
        self.assertIsNone(self.complete({"binding": BINDING}))
        self.assertEqual(self.broker.commands, [])

    def test_restore_transport_failure_is_durable_and_resumes_before_any_apply(self):
        receipt = self.receipt()
        self.broker.fail_apply = self.broker.fail_restore = True
        with self.assertRaisesRegex(ui.PreferencesPending, "rollback_pending"):
            self.complete(receipt)
        self.assertTrue(receipt["post_preferences_progress"]["rollback_required"])
        self.broker.fail_restore = False
        count = sum(action == "apply" for action, _ in self.broker.actions)
        with self.assertRaisesRegex(ui.PreferencesPending, "rolled_back"):
            self.complete(receipt)
        self.assertEqual(sum(action == "apply" for action, _ in self.broker.actions), count)
        self.assertEqual(self.broker.snapshot, initial_snapshot())

    def test_unknown_inflight_apply_outcome_is_observed_without_resend(self):
        receipt = self.receipt()
        target = receipt["post_preferences_plan"]["target"]
        # Model the process dying after applying but before persisting completion.
        self.broker.store = None
        ui._helper(self.broker, self.manager, "apply", target, None)
        receipt["post_preferences_progress"] = {"status": "applying"}
        count = sum(action == "apply" for action, _ in self.broker.actions)
        self.assertTrue(self.complete(receipt)["ok"])
        self.assertEqual(sum(action == "apply" for action, _ in self.broker.actions), count)

    def test_unknown_partial_inflight_apply_is_restored_without_resend(self):
        receipt = self.receipt()
        receipt["post_preferences_progress"] = {"status": "applying"}
        self.broker.snapshot["values"]["font_scale"] = 1.5
        with self.assertRaisesRegex(ui.PreferencesPending, "rolled_back"):
            self.complete(receipt)
        self.assertNotIn("apply", [action for action, _ in self.broker.actions])
        self.assertEqual(self.broker.snapshot, initial_snapshot())

    def test_manual_restore_uses_earliest_snapshot_and_rejects_binding_change(self):
        receipt = self.receipt()
        self.complete(receipt)
        self.capture("later:key")
        self.assertTrue(ui.restore(self.broker, self.manager, BINDING, state_dir=self.root)["verified"])
        self.assertEqual(self.broker.snapshot, initial_snapshot())
        self.broker.binding["resource_tag"] = "c" * 12
        with self.assertRaises(ui.PreferencesPending):
            ui.restore(self.broker, self.manager, state_dir=self.root)

    def test_baseline_symlink_or_public_mode_is_rejected(self):
        self.capture()
        baseline = self.root / (BINDING["instance_id"] + ".baseline.json")
        baseline.chmod(0o644)
        with self.assertRaisesRegex(ui.PreferencesPending, "state_file_invalid"):
            ui.restore(self.broker, self.manager, state_dir=self.root)
        baseline.chmod(0o600)
        other = self.root / "saved.json"
        baseline.rename(other)
        baseline.symlink_to(other)
        with self.assertRaisesRegex(ui.PreferencesPending, "state_file_invalid"):
            ui.restore(self.broker, self.manager, state_dir=self.root)

    def test_helper_copies_only_android_runtime_environment_as_separate_arguments(self):
        self.broker.android_env += b"ANDROID_EXAMPLE=/path/with spaces;$(not-a-command)\0"
        self.capture()
        args = next(command for command in self.broker.commands if ui.HELPER in command)
        self.assertEqual(args[:2], ["env", "-i"])
        self.assertIn("PATH=/system/bin:/system/xbin", args)
        self.assertIn("ANDROID_DATA=/data", args)
        self.assertIn("BOOTCLASSPATH=/apex/runtime/core.jar:/system/framework/framework.jar", args)
        self.assertIn("ANDROID_EXAMPLE=/path/with spaces;$(not-a-command)", args)
        self.assertIn("STANDALONE_SYSTEMSERVER_JARS=/apex/example/service.jar", args)
        self.assertFalse(any("LD_PRELOAD" in arg or "HOST_SECRET" in arg or "IGNORED" in arg for arg in args))

    def test_missing_or_ambiguous_android_environment_stops_before_helper(self):
        for environment in (b"ANDROID_ROOT=/system\0", self.broker.android_env + b"ANDROID_ROOT=/other\0"):
            with self.subTest(environment=environment[:30]):
                self.broker.android_env = environment
                with self.assertRaises(ui.PreferencesPending):
                    self.capture()
                self.assertFalse(self.broker.actions)

    def test_helper_reply_accepts_known_success_notices_only_before_single_final_json(self):
        response = {"ok": True, "snapshot": initial_snapshot()}
        raw = json.dumps(response).encode()
        self.assertEqual(ui._helper_reply(b"boot_id=ok\nnetwork_identity=ok\n" + raw + b"\n"), response)
        for invalid in (b"boot_id=ok\n", b"boot_id=ok" + raw, b"unexpected=ok\n" + raw,
                        b"boot_id=fail:no\n" + raw, b"boot_id=ok\nboot_id=ok\n" + raw,
                        raw + b"\ntrailing", raw + b"\n{}", b'{"ok":true,"ok":false}', b'{"ok":NaN}'):
            with self.subTest(invalid=invalid[:40]), self.assertRaisesRegex(ui.PreferencesPending, "response_invalid"):
                ui._helper_reply(invalid)

    def test_system_server_change_during_environment_read_stops_before_helper(self):
        original = self.broker.native_display_exec
        calls = []
        def execute(manager, args, **options):
            if args == ["pidof", "system_server"]:
                calls.append(1)
                return types.SimpleNamespace(returncode=0, stdout=b"173\n" if len(calls) == 1 else b"174\n")
            return original(manager, args, **options)
        with patch.object(self.broker, "native_display_exec", side_effect=execute):
            with self.assertRaisesRegex(ui.PreferencesPending, "environment_unavailable"):
                self.capture()
        self.assertFalse(self.broker.actions)

    def test_helper_error_json_preserves_native_error_and_rollback_status(self):
        receipt = self.receipt()
        self.broker.fail_apply = True
        self.broker.store = None
        with self.assertRaises(ui.PreferencesPending) as raised:
            ui._helper(self.broker, self.manager, "apply", receipt["post_preferences_plan"]["target"], None)
        self.assertEqual(raised.exception.code, "ui_preferences_setting_failed")
        self.assertTrue(raised.exception.rollback_attempted)
        self.assertFalse(raised.exception.rollback_succeeded)

    def test_settings_subprocess_failures_keep_specific_native_error_codes(self):
        original = self.broker.native_display_exec
        for code in ("ui_preferences_settings_timeout", "ui_preferences_settings_output_limit",
                     "ui_preferences_settings_output_ambiguous", "ui_preferences_settings_command_failed"):
            def execute(manager, args, **options):
                if ui.HELPER in args:
                    return types.SimpleNamespace(returncode=1, stdout=json.dumps({"ok": False, "error": code}).encode())
                return original(manager, args, **options)
            with self.subTest(code=code), patch.object(self.broker, "native_display_exec", side_effect=execute):
                with self.assertRaises(ui.PreferencesPending) as raised:
                    ui._helper(self.broker, self.manager, "read", None, None)
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
