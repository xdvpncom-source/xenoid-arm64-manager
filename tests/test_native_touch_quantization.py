"""Official input must quantize into a cropped viewport without blind clamping."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import script_host as h

DUMP = Path(__file__).with_name('test_native_input_204_fixture.txt').read_text()
PROFILE = {'input': {'name': 'sec_touchscreen',
                     'x': {'minimum': 0, 'maximum': 203},
                     'y': {'minimum': 0, 'maximum': 363}}}


class NativeTouchQuantizationTests(unittest.TestCase):
    def test_live_204_crop_uses_legal_neighbor_for_left_edge(self):
        for y in (0, 181, 363):
            with self.subTest(y=y):
                xraw, yraw, mapping = h.native_touch_point(DUMP, PROFILE, 0, y)
                self.assertEqual(xraw, 1)
                self.assertEqual(yraw, y)
                self.assertEqual(mapping['logical_size'], [204, 364])
                reprojection = .2845 * xraw * (720 / 204) - .2845
                self.assertGreaterEqual(reprojection, 0)
                self.assertLessEqual(reprojection, 1)

    def test_live_nine_points_remain_bounded_and_deterministic(self):
        for y in (0, 181, 363):
            for x in (0, 101, 203):
                with self.subTest(x=x, y=y):
                    first = h.native_touch_point(DUMP, PROFILE, x, y)
                    self.assertEqual(first, h.native_touch_point(DUMP, PROFILE, x, y))
                    rx, ry, unused = first
                    self.assertLessEqual(abs(.2845 * rx * (720 / 204) - .2845 - x), 1)
                    self.assertLessEqual(abs(.2844 * ry * (1280 / 364) - y), 1)
                    self.assertTrue(1 <= rx <= 203 and 0 <= ry <= 363)

    def test_distant_crop_does_not_blindly_clamp_to_first_valid_coordinate(self):
        bad = DUMP.replace('PhysicalLeft: 1', 'PhysicalLeft: 10')
        with self.assertRaisesRegex(h.TransportUnavailable, 'no representable native pixel'):
            h.native_touch_point(bad, PROFILE, 0, 181)

    def test_neighbor_exceeding_one_logical_pixel_is_rejected(self):
        bad = DUMP.replace('PhysicalLeft: 1', 'PhysicalLeft: 0')
        bad = bad.replace('0.2845  -0.0000  -0.2845', '0.8000  -0.0000  0.0000')
        with self.assertRaisesRegex(h.TransportUnavailable, 'no representable native pixel'):
            h.native_touch_point(bad, PROFILE, 4, 181)

    def test_outside_raw_bounds_cannot_be_repaired_by_quantization(self):
        bad = DUMP.replace('0.2845  -0.0000  -0.2845', '0.2845  -0.0000  -20.0000')
        with self.assertRaisesRegex(h.TransportUnavailable, 'no representable native pixel'):
            h.native_touch_point(bad, PROFILE, 203, 181)

    def test_existing_orientation_and_profile_validation_is_preserved(self):
        bad = copy.deepcopy(PROFILE)
        bad['input']['x']['maximum'] = 204
        with self.assertRaisesRegex(h.TransportUnavailable, 'matching the current native profile'):
            h.native_touch_point(DUMP, bad, 101, 181)
        with self.assertRaisesRegex(h.TransportUnavailable, 'invalid reader display geometry'):
            h.native_touch_point(DUMP.replace('InputDeviceOrientation: 0', 'InputDeviceOrientation: 4'), PROFILE, 101, 181)


if __name__ == '__main__':
    unittest.main()
