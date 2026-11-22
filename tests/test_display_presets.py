"""Atomic native display presets, legacy policy compatibility and runtime proof."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
import qa_device_profile as q
import qa_profile_runtime as runtime


class DisplayPresetTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((Path(__file__).parent / "fixtures/native-profile-template.json").read_text())
        self.resources = {"enabled": True, "fps": 20, "scale": "minimum", "min_side": 200,
                          "dpi": 72, "physical_size": [720, 1280]}
        self.policy = q.default_policy([self.base], self.resources)
        self.previous = q.normalize_resources(self.base, self.resources)

    def generate(self, key="preset-test", previous=None):
        return q.generate_profile(self.base, self.policy, key,
                                  previous=self.previous if previous is None else previous,
                                  resource_config=self.resources)

    def test_five_low_load_defaults_and_input_are_atomic(self):
        self.assertEqual(len(self.policy["display_presets"]["values"]), 5)
        previous = self.previous
        seen = set()
        for index in range(30):
            result = self.generate(str(index), previous)
            preset, profile = result["display_preset"], result["profile"]
            self.assertIn(preset, q.DEFAULT_DISPLAY_PRESETS)
            self.assertNotEqual((preset["width"], preset["height"]), (previous["display"]["width"], previous["display"]["height"]))
            self.assertEqual(profile["display"]["densityDpi"], preset["dpi"])
            self.assertEqual(profile["display"]["defaultRefreshRateHz"], 20)
            self.assertEqual(profile["display"]["peakRefreshRateHz"], 20)
            for axis, value in (("x", preset["width"]), ("y", preset["height"])):
                self.assertEqual(profile["input"][axis], {"minimum": 0, "maximum": value - 1})
            seen.add((preset["width"], preset["height"]))
            previous = profile
        self.assertGreaterEqual(len(seen), 4)

    def test_preset_replay_is_deterministic(self):
        self.assertEqual(self.generate(), self.generate())

    def test_different_size_takes_priority_over_only_dpi_or_fps_change(self):
        self.policy["display_presets"]["values"] = [
            {"width": 200, "height": 356, "dpi": 90, "fps": 25},
            {"width": 216, "height": 384, "dpi": 72, "fps": 20}]
        for index in range(10): self.assertEqual(self.generate(str(index))["display_preset"]["width"], 216)

    def test_custom_odd_size_and_rate_use_exact_native_input_bounds(self):
        self.policy["display_presets"]["values"] = [{"width": 321, "height": 517, "dpi": 119, "fps": 25}]
        result = self.generate(); profile = result["profile"]
        self.assertEqual(result["display_source"], "presets")
        self.assertEqual(profile["input"]["x"], {"minimum": 0, "maximum": 320})
        self.assertEqual(profile["input"]["y"], {"minimum": 0, "maximum": 516})
        self.assertEqual(profile["display"]["defaultRefreshRateHz"], 25)

    def test_old_scalar_choices_are_retained_but_inactive(self):
        self.policy["fields"]["display.width"] = {"enabled": True, "values": [1000]}
        before = copy.deepcopy(self.policy["fields"])
        parsed = q.validate_policy(self.policy, [self.base])
        self.assertEqual(parsed["fields"], before)
        self.assertLess(self.generate()["profile"]["display"]["width"], 1000)

    def test_freeze_preserves_full_current_tuple_even_without_previous_argument(self):
        self.policy["display_presets"] = {"enabled": False, "values": []}
        for previous in (self.previous, self.base):
            result = q.generate_profile(self.base, self.policy, "frozen", previous=previous, resource_config=self.resources)
            self.assertEqual(result["profile"]["display"], previous["display"])
            self.assertEqual(result["profile"]["input"], previous["input"])
            self.assertEqual(result["display_source"], "frozen")
        result = q.generate_profile(self.base, self.policy, "frozen", resource_config=self.resources)
        self.assertEqual(result["profile"]["display"], self.base["display"])

    def test_absent_group_keeps_legacy_generation_and_validation(self):
        del self.policy["display_presets"]
        self.assertNotIn("display_presets", q.validate_policy(self.policy, [self.base]))
        result = self.generate()
        self.assertEqual(result["display_source"], "legacy")
        self.assertEqual(result["profile"]["display"]["width"], 202)

    def test_catalog_separates_derived_rows_and_selected_fields(self):
        managed = [row for row in q.field_catalog(self.base, self.policy) if row.get("managed_by")]
        self.assertEqual({row["path"] for row in managed}, q.DISPLAY_PRESET_MANAGED)
        self.assertTrue(all(not row["mutable"] for row in managed))
        self.assertTrue(next(row for row in q.field_catalog(self.base) if row["path"] == "display.width")["mutable"])
        self.assertTrue(set(q.DISPLAY_PRESET_FIELDS) <= set(q.selected_fields(self.base, self.policy)))
        self.policy["display_presets"]["enabled"] = False
        self.assertFalse(set(q.DISPLAY_PRESET_FIELDS) & set(q.selected_fields(self.base, self.policy)))

    def test_explicit_upgrade_adds_presets_but_preserves_existing_custom_group(self):
        old = copy.deepcopy(self.policy); del old["display_presets"]; old["revision"] = 3
        upgraded = q.migrate_policy(old, [self.base], self.resources)
        self.assertEqual(upgraded["revision"], 4)
        self.assertEqual(upgraded["display_presets"]["values"], q.DEFAULT_DISPLAY_PRESETS)
        custom = {"enabled": False, "values": [{"width": 300, "height": 500, "dpi": 90, "fps": 24}]}
        old["display_presets"] = custom
        self.assertEqual(q.migrate_policy(old, [self.base], self.resources)["display_presets"], custom)

    def test_invalid_duplicate_or_empty_enabled_presets_are_rejected(self):
        invalid = [[], [q.DEFAULT_DISPLAY_PRESETS[0]] * 2]
        for key, value in (("width", 1), ("height", 32768), ("dpi", True), ("fps", 0)):
            preset = dict(q.DEFAULT_DISPLAY_PRESETS[0]); preset[key] = value; invalid.append([preset])
        for rows in invalid:
            self.policy["display_presets"] = {"enabled": True, "values": rows}
            with self.subTest(rows=rows), self.assertRaises(ValueError): q.validate_policy(self.policy, [self.base])

    def test_new_legacy_scalar_choices_also_require_nonzero_native_axis_span(self):
        del self.policy["display_presets"]
        for path in ("display.width", "display.height"):
            candidate = copy.deepcopy(self.policy)
            candidate["fields"][path]["values"] = [1]
            with self.subTest(path=path), self.assertRaises(ValueError): q.validate_policy(candidate, [self.base])


class DisplayPresetRuntimeTests(unittest.TestCase):
    def fixture(self):
        import test_qa_profile_runtime as fixture
        value = fixture.RuntimeTest(); value.setUp(); self.addCleanup(value.doCleanups)
        value.policy["display_presets"] = {"enabled": True, "values": [
            {"width": 208, "height": 370, "dpi": 74, "fps": 20},
            {"width": 216, "height": 384, "dpi": 76, "fps": 25}]}
        for row in q.field_catalog(value.base):
            if row["mutable"]: value.policy["fields"][row["path"]]["enabled"] = False
        value.policy_file.write_text(json.dumps(value.policy))
        value.receipt["profile_plan"] = runtime.capture_plan(value.broker, value.manager, value.manager.binding, None,
            config_path=value.policy_file, resource_path=value.resource_file)
        return value

    def test_preset_only_policy_proves_actual_size_fps_and_native_input_before_publish(self):
        value = self.fixture(); result = value.complete(); profile = value.manager.profile
        self.assertIn(profile["display"]["width"], (208, 216))
        self.assertEqual(value.broker.frame_calls[-1], profile["display"]["defaultRefreshRateHz"])
        self.assertEqual(result["display_source"], "presets")
        self.assertTrue(result["display_input"]["ok"])
        self.assertFalse(result["display_input"]["events_injected"])
        state = runtime.load_applied(value.manager.context.instance_id, state_dir=value.root / "state")
        self.assertEqual(state["field_audit"]["display_input"], result["display_input"])
        self.assertEqual(set(result["field_audit"]["fields"]), set(q.DISPLAY_PRESET_FIELDS))

    def test_bad_native_geometry_keeps_profile_pending_and_reuses_same_target(self):
        value = self.fixture(); value.broker.bad_display_input = True
        with self.assertRaisesRegex(runtime.ProfilePending, "native_display_geometry_pending"): value.complete()
        self.assertIsNone(runtime.load_applied(value.manager.context.instance_id, state_dir=value.root / "state"))
        target = value.receipt["profile_progress"]["target_sha256"]
        value.broker.bad_display_input = False
        self.assertEqual(value.complete()["profile_sha256"], target)
        self.assertEqual(value.manager.actual_reloads, 1)

    def test_single_fixed_preset_is_an_explicit_intentional_noop(self):
        value = self.fixture()
        value.policy["display_presets"]["values"] = [{"width": 200, "height": 356, "dpi": 72, "fps": 20}]
        value.policy_file.write_text(json.dumps(value.policy))
        value.receipt["profile_plan"] = runtime.capture_plan(value.broker, value.manager, value.manager.binding, None,
            config_path=value.policy_file, resource_path=value.resource_file)
        result = value.complete()
        self.assertEqual(result["randomization_skipped"], "fixed_display_only")
        self.assertEqual(result["effective_changes_verified"], [])
        self.assertTrue(result["display_input"]["ok"])

    def test_frozen_presets_keep_current_tuple_and_still_verify_native_geometry(self):
        value = self.fixture()
        value.policy["display_presets"]["enabled"] = False
        value.policy_file.write_text(json.dumps(value.policy))
        before = copy.deepcopy(value.manager.profile)
        value.receipt["profile_plan"] = runtime.capture_plan(value.broker, value.manager, value.manager.binding, None,
            config_path=value.policy_file, resource_path=value.resource_file)
        result = value.complete()
        self.assertEqual(value.manager.profile["display"], before["display"])
        self.assertEqual(value.manager.profile["input"], before["input"])
        self.assertEqual(result["display_source"], "frozen")
        self.assertEqual(result["randomization_skipped"], "no_enabled_fields")
        self.assertTrue(result["display_input"]["ok"])
        self.assertGreater(value.broker.input_checks, 0)


if __name__ == "__main__": unittest.main()
