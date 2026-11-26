"""Android 13 exit enums are valid observations, with unknown causes visible."""
from __future__ import annotations

import ast
import copy
import types
import unittest

import test_google_exit_timestamp_locales as timestamp_fixtures

CONSTANTS = timestamp_fixtures.CONSTANTS
OWNER = timestamp_fixtures.OWNER


def history(entries):
    return timestamp_fixtures.TimestampLocaleTests().history(entries)


class ExitReasonCoverageTests(unittest.TestCase):
    def test_android_13_exit_enum_coverage_is_complete(self):
        self.assertEqual(set(CONSTANTS["_EXIT_REASON_BY_CODE"]), set(range(17)))
        self.assertEqual(CONSTANTS["_EXIT_REASON_BY_CODE"][0], "UNKNOWN")
        self.assertEqual(CONSTANTS["_EXIT_REASON_BY_CODE"][8], "PERMISSION_CHANGE")

    def test_valid_old_unknown_exits_are_not_invalid_data_and_remain_reported(self):
        result = history([(-6000, 0), (-7000, 0)])
        self.assertEqual(result, {"ok": True, "code": None, "detail": None, "unknownExitCount": 2})

    def test_current_unknown_exits_are_not_invented_crashes(self):
        result = history([(-100, 0), (-90, 0), (-80, 0)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["unknownExitCount"], 3)

    def test_valid_permission_changes_are_not_crashes(self):
        result = history([(-100, 8), (-90, 8)])
        self.assertEqual(result, {"ok": True, "code": None, "detail": None})

    def test_unknown_and_permission_future_timestamps_still_reject(self):
        for reason in (0, 8):
            with self.subTest(reason=reason):
                result = history([(5, reason)])
                self.assertFalse(result["ok"])
                self.assertEqual(result["detail"], "exitInfoUnavailable")
                self.assertNotIn("unknownExitCount", result)

    def test_unknown_does_not_hide_known_anr_or_crash_loop(self):
        result = history([(-500, 0), (-20, 6)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "com.android.vending:ANR")
        self.assertEqual(result["unknownExitCount"], 1)
        result = history([(-500, 0), (-100, 4), (-90, 4)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "com.android.vending:crashLoop")
        self.assertEqual(result["unknownExitCount"], 1)

    def test_unrecognized_reason_is_not_treated_as_android_unknown(self):
        for offset in (-6000, -10):
            with self.subTest(offset=offset):
                result = history([(offset, 999)])
                self.assertFalse(result["ok"])
                self.assertEqual(result["detail"], "exitInfoUnavailable")

    def test_no_unknown_field_when_no_unknown_records_exist(self):
        self.assertNotIn("unknownExitCount", history([]))
        self.assertNotIn("unknownExitCount", history([(-100, 1)]))

    def test_process_stability_preserves_unknown_count(self):
        method = copy.deepcopy(next(n for n in OWNER.body if isinstance(n, ast.FunctionDef)
                                    and n.name == "_microg_process_stability"))
        scope = {"time": types.SimpleNamespace(sleep=lambda _: None, monotonic=lambda: 0)}
        tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                method], type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), "real-process-stability", "exec"), scope)
        model = types.SimpleNamespace(_microg_process_identity=lambda _: (123, 456),
                                      _microg_exit_history=lambda *_: history([(-6000, 0)]))
        result = scope["_microg_process_stability"](model, {}, 100)
        self.assertTrue(result["ok"])
        self.assertEqual(result["unknownExitCount"], 1)


if __name__ == "__main__": unittest.main()
