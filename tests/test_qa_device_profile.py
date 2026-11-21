"""Native profile coherence, deterministic replay and administrator selection."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
import qa_device_profile as q


class ProfilePolicyTest(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((Path(__file__).parent / "fixtures/native-profile-template.json").read_text())
        self.resources = {"enabled": True, "fps": 20, "scale": "minimum", "min_side": 200, "dpi": 72, "physical_size": [720, 1280], "physical_density": 560}
        self.policy = q.default_policy([self.base], self.resources)

    def generate(self, key="host22-device6-operation1", **kwargs):
        return q.generate_profile(self.base, self.policy, key, resource_config=self.resources, **kwargs)

    def test_actual_native_template_validates(self):
        self.assertEqual(q.validate_profile(self.base), self.base)

    def test_all_editable_fields_default_enabled_and_native_ids_are_locked(self):
        catalog = q.field_catalog(self.base)
        self.assertTrue(all(self.policy["fields"][d["path"]]["enabled"] for d in catalog))
        ids = [d for d in catalog if d["native_managed"]]
        self.assertEqual(len(ids), 6)
        self.assertTrue(all(not d["mutable"] and self.policy["fields"][d["path"]]["values"] == [] for d in ids))

    def test_every_editable_default_has_alternatives_and_changes(self):
        mutable = [d["path"] for d in q.field_catalog(self.base) if d["mutable"]]
        self.assertEqual(len(mutable), 39)
        for path in mutable:
            self.assertGreaterEqual(len(self.policy["fields"][path]["values"]), 2, path)
        previous = q.normalize_resources(self.base, self.resources)
        for index in range(10):
            result = self.generate("all-checked-" + str(index), previous=previous)
            self.assertEqual(result["unchanged_enabled_fields"], [])
            for path in mutable:
                self.assertNotEqual(q._get(result["profile"], path), q._get(previous, path), path)
            previous = result["profile"]

    def test_request_replay_is_byte_identical(self):
        first, second = self.generate(), self.generate()
        self.assertEqual(first, second)
        self.assertEqual(q.canonical_bytes(first["profile"]), q.canonical_bytes(second["profile"]))

    def test_different_operation_changes_supported_profile_but_keeps_build(self):
        first, second = self.generate(), self.generate("another-device-or-operation")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["profile"]["build"], self.base["build"])
        self.assertEqual(second["profile"]["build"], self.base["build"])
        self.assertFalse(first["qa_build_metadata"])

    def test_profile_hardware_family_and_os_abi_are_retained(self):
        got = self.generate()["profile"]
        for name in ("brand", "manufacturer", "model", "device", "product", "release", "sdk", "abi", "abilist", "board", "soc_model"):
            self.assertEqual(got["build"][name], self.base["build"][name])
        self.assertEqual(got["storage"], self.base["storage"])

    def test_full_document_keeps_unknown_sections(self):
        self.base["new_future_metadata"] = {"preserve": [1, "safe", True]}
        self.policy = q.default_policy([self.base], self.resources)
        got = self.generate()["profile"]
        self.assertEqual(got["new_future_metadata"], self.base["new_future_metadata"])
        self.assertEqual(got["sensors"], self.base["sensors"])
        self.assertEqual(got["cameras"], self.base["cameras"])

    def test_previous_document_lets_display_alternate_without_renormalizing(self):
        first = self.generate()["profile"]
        second = self.generate("next-round", previous=first)["profile"]
        self.assertEqual({first["display"]["width"], second["display"]["width"]}, {200, 202})
        self.assertEqual({first["display"]["densityDpi"], second["display"]["densityDpi"]}, {72, 74})
        for got in (first, second):
            self.assertEqual(got["input"]["x"]["maximum"], got["display"]["width"] - 1)
            self.assertEqual(got["input"]["y"]["maximum"], got["display"]["height"] - 1)
            self.assertIn(got["display"]["defaultRefreshRateHz"], (20, 21))
            q.validate_profile(got)

    def test_preview_uses_explicit_actual_runtime_display_previous(self):
        got = self.generate(previous=q.normalize_resources(self.base, self.resources))["profile"]
        self.assertIn(got["display"]["width"], (200, 202))
        self.assertIn(got["display"]["height"], (356, 360))
        self.assertIn(got["display"]["densityDpi"], (72, 74))
        self.assertIn(got["display"]["defaultRefreshRateHz"], (20, 21))

    def test_disabled_edited_display_candidates_never_replace_actual_previous(self):
        previous = q.normalize_resources(self.base, self.resources)
        previous["display"].update(width=202, height=360, densityDpi=74)
        previous["input"]["x"]["maximum"] = 201
        previous["input"]["y"]["maximum"] = 359
        edits = {"display.width": 240, "display.height": 428, "display.densityDpi": 80, "input.x.maximum": 239, "input.y.maximum": 427}
        for path, value in edits.items():
            self.policy["fields"][path] = {"enabled": False, "values": [value]}
        got = self.generate(previous=previous)["profile"]
        for path in edits:
            self.assertEqual(q._get(got, path), q._get(previous, path))

    def test_first_applied_profile_changes_both_refresh_fields_coherently(self):
        previous = q.normalize_resources(self.base, self.resources)
        previous["display"]["peakRefreshRateHz"] = 120
        for index in range(10):
            got = self.generate("first-fps-" + str(index), previous=previous)["profile"]
            self.assertEqual(got["display"]["defaultRefreshRateHz"], 21)
            self.assertEqual(got["display"]["peakRefreshRateHz"], 21)

    def test_compiled_sensor_camera_and_mode_catalogs_remain_descriptive(self):
        got = self.generate()["profile"]
        for key in ("sensors", "cameras"):
            self.assertEqual(got[key], self.base[key])
        self.assertEqual(got["display"]["modes"], self.base["display"]["modes"])
        self.assertEqual(got["display"]["supportedRefreshRatesHz"], self.base["display"]["supportedRefreshRatesHz"])
        catalog = {d["path"]: d for d in q.field_catalog(self.base)}
        self.assertFalse(catalog["build.kernel_build_suffix"]["mutable"])
        self.assertFalse(catalog["sensors.0.name"]["mutable"])

    def test_json_unsupported_build_fields_are_readonly(self):
        for path in ("build.id", "build.incremental", "build.fingerprint", "build.description"):
            self.assertFalse(next(d for d in q.field_catalog(self.base) if d["path"] == path)["mutable"])
            self.assertEqual(self.policy["fields"][path]["values"], [])

    def test_disabled_input_limit_blocks_incompatible_dimension(self):
        self.policy["fields"]["input.x.maximum"]["enabled"] = False
        normalized = q.normalize_resources(self.base, self.resources)
        got = self.generate()["profile"]
        self.assertEqual(got["input"]["x"], normalized["input"]["x"])
        self.assertEqual(got["display"]["width"], normalized["display"]["width"])

    def test_unchecked_scalar_stays_equal_to_previous_value(self):
        prior = self.generate()["profile"]
        self.policy["fields"]["battery.temperature"]["enabled"] = False
        got = self.generate("next-round", previous=prior)["profile"]
        self.assertEqual(got["battery"]["temperature"], prior["battery"]["temperature"])

    def test_fresh_native_ids_are_used_instead_of_stored_baseline(self):
        fresh = dict(self.base["ids"], android_id="fedcba9876543210", serial="QA11111111111111")
        got = self.generate(native_ids=fresh)["profile"]
        self.assertEqual(got["ids"], fresh)
        self.assertEqual(got["usb"]["serial"], fresh["serial"])

    def test_native_checkbox_cannot_promise_to_disable_official_regen(self):
        self.policy["fields"]["ids.android_id"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "cannot be disabled"):
            self.generate()

    def test_native_or_locked_values_cannot_override_owner(self):
        self.policy["fields"]["ids.android_id"]["values"] = ["aaaaaaaaaaaaaaaa"]
        with self.assertRaisesRegex(ValueError, "native contract"):
            self.generate()

    def test_disabled_policy_is_exact_noop(self):
        self.policy["enabled"] = False
        got = self.generate()
        self.assertEqual(got["profile"], self.base)
        self.assertFalse(got["unique_profile"])

    def test_empty_editable_selection_is_noop_without_resource_override(self):
        for path, row in self.policy["fields"].items():
            if next(d for d in q.field_catalog(self.base) if d["path"] == path)["mutable"]:
                row["enabled"] = False
        got = q.generate_profile(self.base, self.policy, "no-fields")
        self.assertEqual(got["profile"], self.base)

    def test_cpu_clusters_and_battery_invariants_hold_across_many_seeds(self):
        previous = None
        for index in range(25):
            got = self.generate("operation-" + str(index), previous=previous)["profile"]
            q.validate_profile(got)
            battery = got["battery"]
            self.assertEqual(battery["chargeCounterUah"], battery["chargeFullUah"] * battery["level"] // 100)
            previous = got

    def test_schema_unknown_paths_and_nonfinite_values_rejected(self):
        for bad in ({"schema": "not-native-policy"}, {"fields": {"build.mdoel": {"enabled": True, "values": ["x"]}}}, {"fields": {"battery.temperature": {"enabled": True, "values": [float("nan")]}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                q.validate_policy(bad, [self.base])

    def test_bad_native_scalar_candidates_are_rejected_during_policy_save(self):
        for path, value in (("battery.level", 101), ("battery.temperature", "warm"), ("display.width", -1), ("memory.totalBytes", 5001), ("build.fingerprint", "not-a-fingerprint")):
            bad = copy.deepcopy(self.policy)
            bad["fields"][path]["values"] = [value]
            with self.subTest(path=path), self.assertRaises(ValueError):
                q.validate_policy(bad, [self.base])

    def test_location_owned_sections_are_not_accepted_by_fingerprint_apply(self):
        bad = copy.deepcopy(self.base)
        bad["network"] = {"mac": "02:00:00:00:00:01"}
        with self.assertRaisesRegex(ValueError, "Location"):
            q.validate_profile(bad)

    def test_templates_are_literal_replacements_not_code(self):
        self.policy["fields"]["usb.product"]["values"] = ["QA {digits} {token} $(never-executed)"]
        got = self.generate()["profile"]["usb"]["product"]
        self.assertIn("$(never-executed)", got)
        self.assertNotIn("{digits}", got)
        self.policy["fields"]["usb.product"]["values"] = ["{__class__}"]
        with self.assertRaisesRegex(ValueError, "Only"):
            self.generate()

    def test_actual_physical_metrics_required_for_resource_normalization(self):
        config = {key: value for key, value in self.resources.items() if key != "physical_size"}
        with self.assertRaisesRegex(ValueError, "physical_size"):
            q.normalize_resources(self.base, config)

    def test_invalid_native_profiles_are_rejected_before_apply(self):
        cases = []
        for section, key, value in (("storage", "capacityBytes", 64_000_000_000), ("memory", "totalKiB", 4), ("battery", "chargeCounterUah", 1), ("build", "fingerprint", "bad")):
            bad = copy.deepcopy(self.base); bad[section][key] = value; cases.append(bad)
        for bad in cases:
            with self.assertRaises(ValueError): q.validate_profile(bad)


if __name__ == "__main__":
    unittest.main()
