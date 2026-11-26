"""The editor rollback uses official GEN, then the exact stored UI baseline."""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "qa/restore-device-region.py"


def baseline(country="SG"):
    return {"native_country": country, "snapshot": {
        "values": {"locales": ["en-SG"], "timezone": "Asia/Singapore", "font_scale": 1.0, "night_mode": 0},
        "settings": {"system": {"time_12_24": None, "date_format": None, "font_scale": None,
                                  "system_locales": None}, "global": {"auto_time_zone": "1", "xenoid_qa_ui_preferences_v1": None}},
        "properties": {"locale": "en-SG", "timezone": "Asia/Singapore"}}}


class Device:
    def __init__(self, value):
        self.original = copy.deepcopy(value)
        self.calls = []
        self.answers = []
        self.current = None
        self.restore_result = {"ok": True, "restored": True, "verified": True}

    def preferences_baseline(self):
        self.calls.append(("baseline",))
        return copy.deepcopy(self.original)

    def regenerate(self, **kwargs):
        # A different API (ADB, property rewrite, identifier assignment, etc.)
        # is deliberately absent from this fake and would fail the test.
        assert set(kwargs) == {"operation_key", "post_preferences", "post_cache", "post_google"}
        self.calls.append(("regenerate", copy.deepcopy(kwargs)))
        answer = self.answers.pop(0) if self.answers else {"ok": True, "ui_preferences": {"verified": True}}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def restore_preferences(self):
        self.calls.append(("restore",))
        self.current = copy.deepcopy(self.original["snapshot"])
        return self.restore_result

    def log(self, message):
        self.calls.append(("log", message))


class RestoreRegionTest(unittest.TestCase):
    def load(self, device):
        spec = importlib.util.spec_from_file_location("restore_region_fixture", SOURCE)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"xenoid_device": types.SimpleNamespace(Device=lambda: device)}):
            spec.loader.exec_module(module)
        module.time = types.SimpleNamespace(sleep=lambda value: device.calls.append(("sleep", value)))
        module.uuid = types.SimpleNamespace(uuid4=Mock(return_value=types.SimpleNamespace(hex="a" * 32)))
        return module

    def test_original_country_restored_via_one_gen_then_exact_ui(self):
        d = Device(baseline())
        original = copy.deepcopy(d.original)
        self.load(d).main()
        calls = [call for call in d.calls if call[0] == "regenerate"]
        self.assertEqual(len(calls), 1)
        args = calls[0][1]
        self.assertEqual(args["operation_key"], "restore-region-" + "a" * 32)
        self.assertEqual(args["post_preferences"], {
            "enabled": True, "regions": [{"locales": ["en-SG"], "timezone": "Asia/Singapore",
                                           "time_12_24": "24", "date_format": "yyyy-MM-dd"}],
            "font_scales": [1.0], "night_modes": [1],
            "native_location": {"country": "SG", "randomize_carrier": False}})
        self.assertEqual(args["post_cache"]["packages"], ["com.google.android.gms", "com.google.android.gsf"])
        self.assertEqual(args["post_google"]["clear_packages"], args["post_cache"]["packages"])
        self.assertLess(d.calls.index(calls[0]), d.calls.index(("restore",)))
        self.assertEqual(d.current, original["snapshot"])
        self.assertEqual(d.original, original)
        self.assertIn("fresh unique identifiers", [call[1] for call in d.calls if call[0] == "log"][-1])

    def test_old_ui_only_baseline_never_generates_new_identity(self):
        d = Device(baseline(None))
        module = self.load(d)
        module.main()
        self.assertFalse(any(call[0] == "regenerate" for call in d.calls))
        module.uuid.uuid4.assert_not_called()
        self.assertEqual(d.current, d.original["snapshot"])
        self.assertEqual(sum(call[0] == "restore" for call in d.calls), 1)
        self.assertIn("native country unchanged", [call[1] for call in d.calls if call[0] == "log"][-1])

    def test_transient_failure_and_unverified_reply_retry_same_key_and_plan(self):
        d = Device(baseline())
        d.answers = [OSError("transport interrupted"), {"ok": True, "ui_preferences": {"verified": False}},
                     {"ok": True, "ui_preferences": {"verified": True}}]
        module = self.load(d)
        module.main()
        calls = [call[1] for call in d.calls if call[0] == "regenerate"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls, [calls[0]] * 3)
        module.uuid.uuid4.assert_called_once()
        self.assertEqual([call[1] for call in d.calls if call[0] == "sleep"], [15, 15])
        self.assertEqual(sum(call[0] == "restore" for call in d.calls), 1)
        self.assertLess(max(index for index, call in enumerate(d.calls) if call[0] == "regenerate"),
                        next(index for index, call in enumerate(d.calls) if call[0] == "restore"))

    def test_unverified_ui_restore_is_reported_as_failure(self):
        d = Device(baseline(None))
        d.restore_result = {"ok": True, "restored": True, "verified": False}
        with self.assertRaisesRegex(RuntimeError, "settings were not restored"):
            self.load(d).main()


if __name__ == "__main__":
    unittest.main()
