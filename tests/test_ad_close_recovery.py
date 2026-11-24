import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'qa/vpn-cycle.py'
sys.modules['xenoid_device'] = types.SimpleNamespace(Device=object)
spec = importlib.util.spec_from_file_location('ad_candidate', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def close(box=(940, 30, 1000, 90), **changes):
    return dict(resource_id='', text='', content_desc='Close advertisement',
                class_name='android.widget.Button', package='test.vpn',
                enabled=True, clickable=True, bounds=list(box),
                window_id=7, display_id=0, **changes)


def window(*items):
    return {'package': 'test.vpn', 'component': 'test.vpn/com.google.android.gms.ads.AdActivity',
            'stable': True, 'display_id': 0, 'controls': list(items)}


class Device:
    def __init__(self):
        self.lines, self.taps = [], []
    def settings(self): return {'android_user': 0}
    def log(self, line): self.lines.append(line)
    def tap(self, x, y): self.taps.append((x, y))
    def inspect_window(self, **kw): raise AssertionError('No failure-time read')
    def adb(self, *args): raise AssertionError('No ADB input or diagnostics')
    def back(self): raise AssertionError('No key fallback')


class Tests(unittest.TestCase):
    def setUp(self):
        self.d = Device()
        self.c = m.Cycle(self.d, 'test.vpn')

    def test_nested_webview_close_is_one_actionable_target(self):
        outer = close((925, 10, 1020, 115))
        inner = close()
        w = window(outer, inner)
        self.assertEqual(self.c.ad_close_control(w), inner)
        self.c.close_ad_control(w, inner)
        self.assertEqual(self.d.taps, [(970, 60)])

    def test_disjoint_overlapping_and_foreign_window_stay_ambiguous(self):
        a = close()
        for b in (close((30, 30, 90, 90)), close((960, 40, 1020, 100)),
                  {**close((930, 20, 1010, 100)), 'window_id': 8},
                  {**close((930, 20, 1010, 100)), 'package': 'other.app'}):
            self.assertIsNone(self.c.ad_close_control(window(a, b)))
        self.assertFalse(self.d.taps)

    def test_nested_requires_metadata_and_actionable_inner(self):
        outer = close((920, 10, 1030, 120))
        for inner in ({**close(), 'clickable': False, 'class_name': 'android.view.View'},
                      {**close(), 'window_id': None}, {**close(), 'display_id': None}):
            self.assertIsNone(self.c.ad_close_control(window(outer, inner)))

    def test_packed_webview_ids_require_exact_complete_name(self):
        for name in sorted(m.AD_PACKED_CLOSE_IDS):
            item = {**close(), 'content_desc': '', 'resource_id': name}
            self.assertEqual(self.c.ad_close_control(window(item)), item)
        for name in ('disclose', 'adclosed', 'myadclosepurchase', 'adcloseinstall'):
            item = {**close(), 'content_desc': '', 'resource_id': name}
            self.assertIsNone(self.c.ad_close_control(window(item)))

    def test_packed_ids_do_not_override_cta_countdown_or_privacy(self):
        for label in ('Install', 'Close in 5', 'Close account', 'Ad choices', 'Not close'):
            item = {**close(), 'resource_id': 'adclosebutton', 'content_desc': label}
            self.assertIsNone(self.c.ad_close_control(window(item)))

    def test_new_packed_id_on_nonactionable_layout_is_not_a_button(self):
        item = {**close(), 'resource_id': 'adclose', 'content_desc': '',
                'class_name': 'android.view.View', 'clickable': False}
        self.assertIsNone(self.c.ad_close_control(window(item)))

    def test_gms_system_and_webview_semantics_use_same_official_tap(self):
        for rid, label in (('com.google.android.gms:id/close_button', ''),
                           ('android:id/button2', 'Close'), ('mraidclose', ''),
                           ('', 'Interstitial close button')):
            item = {**close(), 'resource_id': rid, 'content_desc': label}
            w = window(item)
            self.c.close_ad_control(w, self.c.ad_close_control(w))
        self.assertEqual(self.d.taps, [(970, 60)] * 4)

    def test_anonymous_corner_and_unexposed_back_are_not_guessed(self):
        item = {**close(), 'resource_id': '', 'content_desc': ''}
        w = window(item)
        self.assertIsNone(self.c.ad_close_control(w))
        with patch.object(m.time, 'monotonic', return_value=1):
            self.assertFalse(self.c.service_missing_ad_close(w))
        with patch.object(m.time, 'monotonic', return_value=30):
            self.assertFalse(self.c.service_missing_ad_close(w))
        self.assertFalse(self.d.taps)

    def test_failure_captures_held_metadata_without_copy_or_urls(self):
        item = {**close(), 'content_desc': 'person@example.com https://private.example/x 42',
                'text': 'private account value', 'resource_id': 'https://private.example/x'}
        w = window(item)
        before = copy.deepcopy(w)
        self.c.last_ad_window = w
        self.c.ad_failure('unavailable')
        self.assertEqual(w, before)
        logged = '\n'.join(self.d.lines)
        for secret in ('person@example.com', 'private.example', 'private account value'):
            self.assertNotIn(secret, logged)
        header = json.loads(self.d.lines[0].split('Ad close evidence ', 1)[1])
        self.assertEqual(header['reason'], 'no_semantic_close')
        self.assertEqual(header['controls_recorded'], 1)
        self.assertFalse(self.d.taps)

    def test_failure_records_ambiguity_and_exhausted_budget(self):
        self.c.ad_failure_evidence(window(close(), close((20, 10, 80, 70))))
        self.assertEqual(json.loads(self.d.lines[0].split('Ad close evidence ', 1)[1])['reason'],
                         'ambiguous_semantic_close')
        self.d.lines.clear()
        w = window(close())
        for _ in range(2): self.c.close_ad_control(w, w['controls'][0])
        self.d.lines.clear()
        self.c.ad_failure_evidence(w)
        self.assertEqual(json.loads(self.d.lines[0].split('Ad close evidence ', 1)[1])['reason'],
                         'close_attempt_budget_exhausted')

    def test_failure_output_is_bounded_and_omissions_truthful(self):
        self.c.ad_failure_evidence(window(*[close() for _ in range(200)]))
        header = json.loads(self.d.lines[0].split('Ad close evidence ', 1)[1])
        self.assertEqual(header['controls_recorded'], len(self.d.lines) - 1)
        self.assertEqual(header['controls_omitted'], 200 - header['controls_recorded'])
        self.assertLess(sum(len(line.encode()) + 1 for line in self.d.lines), 26000)
        self.assertTrue(all(len(line.encode()) < 2048 for line in self.d.lines))


if __name__ == '__main__': unittest.main()
