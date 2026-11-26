"""Live regressions: a fast store redirect and an already-clear late UI read."""
import unittest
from unittest.mock import patch
from test_cycle_ad_windows import Clock, MAIN, Replay, STORE, W_AD, m


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time = patch.object(m, 'time', self.clock)
        self.time.start(); self.addCleanup(self.time.stop)

    def test_missed_fast_redirect_gets_one_official_back_after_completed_dwell(self):
        device = Replay(STORE, on_back=MAIN)
        cycle = m.Cycle(device, 'v2.vpn.fast')
        cycle.post_connect_dwell_complete = True
        cycle.dismiss_ads()
        self.assertEqual([x for x in device.actions if x[0] == 'native_tap'], [('native_tap', 42, 345)])
        self.assertIsNone(cycle.observed_post_connect_ad)
        self.assertIsNone(cycle.ad_landing_origin)

    def test_no_configured_store_back_before_dwell_or_for_unconfigured_package(self):
        device = Replay(STORE); cycle = m.Cycle(device, 'v2.vpn.fast')
        self.assertFalse(cycle.return_from_ad_landing(cycle.observe()))
        cycle.post_connect_dwell_complete = True
        device.window = {**STORE, 'package': 'unconfigured.example'}
        self.assertFalse(cycle.return_from_ad_landing(cycle.observe()))
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])

    def test_unknown_origin_does_not_authorize_a_second_back_from_returned_ad(self):
        device = Replay(STORE, on_back=W_AD)
        cycle = m.Cycle(device, 'v2.vpn.fast'); cycle.post_connect_dwell_complete = True
        self.assertTrue(cycle.return_from_ad_landing(cycle.observe()))
        self.assertIsNone(cycle.ad_landing_origin)
        self.assertFalse(cycle.return_from_ad_landing(cycle.observe()))
        self.assertIsNotNone(cycle.ad_close_control(cycle.observe()))

    def late_read(self, next_window):
        device = Replay(MAIN)
        cycle = m.Cycle(device, 'v2.vpn.fast')
        original = device.inspect_window
        reads = []
        def inspect(**kwargs):
            if not reads:
                self.clock.now += 20  # First UI read crosses the five-second budget.
            else:
                device.window = next_window
            reads.append(1)
            return original(**kwargs)
        device.inspect_window = inspect
        return device, cycle, reads

    def test_clear_app_at_expired_deadline_gets_fresh_readonly_confirmation(self):
        device, cycle, reads = self.late_read(MAIN)
        with patch.object(m, 'AD_READY_SECONDS', 5):
            cycle.dismiss_ads()
        self.assertEqual(len(reads), 2)
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])
        self.assertTrue(any('confirmed clear' in x for x in device.logs))

    def test_late_ad_return_is_not_declared_clear_or_given_extra_input(self):
        device, cycle, reads = self.late_read(W_AD)
        with patch.object(m, 'AD_READY_SECONDS', 5):
            with self.assertRaisesRegex(RuntimeError, 'usable close control'):
                cycle.dismiss_ads()
        self.assertEqual(len(reads), 2)
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])

    def test_prepare_screen_confirms_late_clear_window_and_returns_fresh_snapshot(self):
        device, cycle, reads = self.late_read(MAIN)
        result = cycle.prepare_screen(5)
        self.assertEqual(result['component'], MAIN['component'])
        self.assertEqual(len(reads), 2)
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])

    def test_prepare_screen_late_returning_ad_still_fails_without_input(self):
        device, cycle, reads = self.late_read(W_AD)
        with self.assertRaisesRegex(RuntimeError, 'unobstructed foreground'):
            cycle.prepare_screen(5)
        self.assertEqual(len(reads), 2)
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])

    def test_late_unstable_app_does_not_gain_confirmation_budget(self):
        device, cycle, reads = self.late_read(MAIN)
        device.window = {**MAIN, 'stable': False}
        with patch.object(m, 'AD_READY_SECONDS', 5):
            with self.assertRaisesRegex(RuntimeError, 'usable close control'):
                cycle.dismiss_ads()
        self.assertEqual(len(reads), 1)

    def test_confirmation_read_exceeding_its_own_budget_cannot_report_success(self):
        device, cycle, reads = self.late_read(MAIN)
        original = device.inspect_window
        def inspect(**kwargs):
            if reads:
                self.clock.now += 31
            return original(**kwargs)
        device.inspect_window = inspect
        with patch.object(m, 'AD_READY_SECONDS', 5):
            with self.assertRaisesRegex(RuntimeError, 'usable close control'):
                cycle.dismiss_ads()
        self.assertEqual(len(reads), 2)
        self.assertFalse([x for x in device.actions if x[0] == 'native_tap'])


if __name__ == '__main__':
    unittest.main()
