"""Read-only mapping of a redacted real cold Android identity-transform dump."""
import ast
import math
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa'))
import script_host as h

DUMP = (ROOT / 'tests/fixtures/native-cold-identity-input.txt').read_text()
PROFILE = {'input': {'name': 'sec_touchscreen',
                    'x': {'minimum': 0, 'maximum': 1079},
                    'y': {'minimum': 0, 'maximum': 2399}}}
IDENTITY = '        transform (ROT_0) (IDENTITY)'


class ColdIdentityInputTests(unittest.TestCase):
    def test_real_cold_corners_and_center_keep_exact_official_coordinates(self):
        for x, y in ((0, 0), (1079, 0), (0, 2399), (1079, 2399), (539, 1199)):
            with self.subTest(point=(x, y)):
                rx, ry, mapping = h.native_touch_point(DUMP, PROFILE, x, y)
                self.assertEqual((rx, ry), (x, y))
                self.assertEqual(mapping, {'device_id': 3, 'display_id': 0,
                                          'logical_size': [1080, 2400], 'orientation': 0})
        self.assertEqual(h.native_touch_point(DUMP, PROFILE, None, None)[:2], (539, 1199))

    def test_rotated_numeric_matrix_still_maps_each_corner(self):
        numeric = ('        transform (ROT_90) (ROTATE TRANSLATE)\n'
                   '            0.0000 -1.0000 2399.0000\n'
                   '            1.0000 0.0000 0.0000\n'
                   '            0.0000 0.0000 1.0000')
        dump = DUMP.replace('logicalSize=1080x2400', 'logicalSize=2400x1080').replace(IDENTITY, numeric)
        for x, y in ((0, 0), (2399, 0), (0, 1079), (2399, 1079), (1199, 539)):
            with self.subTest(point=(x, y)):
                rx, ry, mapping = h.native_touch_point(dump, PROFILE, x, y)
                self.assertEqual((rx, ry), (y, 2399-x))
                self.assertEqual(mapping['logical_size'], [2400, 1080])

    def test_missing_duplicate_unknown_or_nonidentity_without_rows_rejected(self):
        replacements = ('', IDENTITY + '\n' + IDENTITY,
                        '        transform (ROT_0) (UNKNOWN)',
                        '        transform (ROT_90) (IDENTITY)',
                        '        transform (ROT_0) (SCALE)',
                        IDENTITY + '\n            1 0 0',
                        IDENTITY + '\n            unexpected')
        for replacement in replacements:
            with self.subTest(replacement=replacement), self.assertRaises(h.TransportUnavailable):
                h.native_touch_point(DUMP.replace(IDENTITY, replacement), PROFILE, None, None)

    def test_existing_scaled_numeric_fixture_and_profile_guard_remain_strict(self):
        dump = (ROOT / 'tests/test_native_input_204_fixture.txt').read_text()
        profile = {'input': {'name': 'sec_touchscreen', 'x': {'minimum': 0, 'maximum': 203},
                             'y': {'minimum': 0, 'maximum': 363}}}
        self.assertEqual(h.native_touch_point(dump, profile, 0, 181)[:2], (1, 181))
        bad = {'input': {**PROFILE['input'], 'x': {'minimum': 0, 'maximum': 1080}}}
        with self.assertRaises(h.TransportUnavailable):
            h.native_touch_point(DUMP, bad, None, None)

    def test_panel_mirror_handles_identity_without_changing_its_existing_quantization(self):
        panel = ROOT.parent / 'panel-fresh6a-release/script_host.py'
        if not panel.is_file():
            self.skipTest('separate panel checkout not available')
        node = next(n for n in ast.parse(panel.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'native_touch_point')
        namespace = {'re': re, 'math': math, 'TransportUnavailable': h.TransportUnavailable}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(panel), 'exec'), namespace)
        parse = namespace['native_touch_point']
        for x, y in ((0, 0), (1079, 0), (0, 2399), (1079, 2399), (539, 1199)):
            self.assertEqual(parse(DUMP, PROFILE, x, y)[:2], (x, y))
        for replacement in ('', IDENTITY+'\n'+IDENTITY,
                            '        transform (ROT_0) (UNKNOWN)',
                            '        transform (ROT_90) (ROTATE)'):
            with self.assertRaises(h.TransportUnavailable):
                parse(DUMP.replace(IDENTITY, replacement), PROFILE, None, None)


if __name__ == '__main__':
    unittest.main()
