"""Only a completed VPN dwell can recover an observed automatic ad redirect."""
import copy
import unittest
from unittest.mock import patch
from test_cycle_ad_windows import Clock, MAIN, Replay, STORE, W_AD, m


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = Replay(W_AD, on_tap=MAIN, on_back=W_AD, on_ad_back=MAIN)
        self.cycle = m.Cycle(self.device, 'v2.vpn.fast')
        self.time = patch.object(m, 'time', self.clock)
        self.time.start(); self.addCleanup(self.time.stop)
        self.budget = patch.object(m, 'AD_READY_SECONDS', 16)
        self.budget.start(); self.addCleanup(self.budget.stop)
        def focus(args):
            self.assertEqual(args, m.CRASH_FOCUS_COMMAND)
            if self.clock.now >= 20 and self.device.window is W_AD:
                self.device.window = STORE
            return 'mCurrentFocus=Window{1 u0 ' + self.device.window['component'] + '}'
        self.device.adb = focus

    def completed_dwell(self, duration=96):
        self.cycle.post_connect_dwell_active = True
        self.cycle.wait(duration, dialogs=True, vpn=True, ads=False)
        self.cycle.post_connect_dwell_active = False
        self.cycle.post_connect_dwell_complete = True

    def taps(self):
        return [row for row in self.device.actions if row[0] == 'native_tap']

    def test_automatic_redirect_waits_full_dwell_then_back_and_real_ad_close(self):
        self.completed_dwell()
        self.assertEqual(self.clock.now, 96)
        self.assertFalse(self.taps())
        self.assertIsNone(self.cycle.last_ad_close, 'An observation must never fabricate a click')
        self.assertIsNotNone(self.cycle.observed_post_connect_ad)
        self.device.adb = lambda args: 'mCurrentFocus=Window{1 u0 ' + self.device.window['component'] + '}'
        self.cycle.dismiss_ads()
        self.assertEqual(self.taps(), [('native_tap',42,345),('native_tap',189,10)])
        self.assertEqual(self.device.window, MAIN)
        self.assertTrue(any('Observed post-Connect ad reached' in line for line in self.device.logs))

    def test_early_store_during_dwell_never_sends_back(self):
        self.cycle.post_connect_dwell_active = True
        self.cycle.observe_post_connect_ad(self.cycle.observe())
        self.device.window = STORE
        self.assertFalse(self.cycle.return_from_ad_landing(self.cycle.observe()))
        self.assertFalse(self.taps())

    def test_unrelated_store_without_observed_owned_ad_is_not_recovered(self):
        self.device.window = STORE
        self.completed_dwell()
        with patch.object(m, 'AD_LANDING_REQUIRE_OBSERVED_AD', True), self.assertRaisesRegex(RuntimeError, 'usable close control'):
            self.cycle.dismiss_ads()
        self.assertIsNone(self.cycle.observed_post_connect_ad)
        self.assertFalse(self.taps())

    def test_foreign_ad_cannot_authorize_store_back(self):
        self.device.window = {**W_AD, 'package': 'other.app', 'component': 'other.app/AdActivity'}
        self.cycle.post_connect_dwell_active = True
        self.cycle.observe_post_connect_ad(self.cycle.observe())
        self.cycle.post_connect_dwell_complete = True
        self.device.window = STORE
        with patch.object(m, 'AD_LANDING_REQUIRE_OBSERVED_AD', True):
            self.assertFalse(self.cycle.return_from_ad_landing(self.cycle.observe()))
        self.assertIsNone(self.cycle.observed_post_connect_ad)
        self.assertFalse(self.taps())

    def test_unpaired_focus_is_not_origin_proof(self):
        self.cycle.post_connect_dwell_active = True
        current = self.cycle.observe(); current['focus_observation']['exact_match'] = False
        self.cycle.observe_post_connect_ad(current)
        self.assertIsNone(self.cycle.observed_post_connect_ad)

    def test_expired_origin_is_not_relabelled_as_fresh(self):
        self.cycle.post_connect_dwell_active = True
        self.cycle.observe_post_connect_ad(self.cycle.observe())
        at = self.cycle.observed_post_connect_ad['at']
        self.cycle.post_connect_dwell_complete = True
        self.clock.now = max(m.START_WAIT_SECONDS) + m.CONNECTED_WAIT_SECONDS + m.AD_LANDING_PROVENANCE_SECONDS + 1
        self.device.window = STORE
        with patch.object(m, 'AD_LANDING_REQUIRE_OBSERVED_AD', True):
            self.assertFalse(self.cycle.return_from_ad_landing(self.cycle.observe()))
        self.assertEqual(self.cycle.observed_post_connect_ad['at'], at)
        self.assertFalse(self.taps())

    def test_full120_second_dwell_keeps_early_observation_without_retiming(self):
        self.cycle.post_connect_dwell_active = True
        self.cycle.observe_post_connect_ad(self.cycle.observe())
        at = self.cycle.observed_post_connect_ad['at']
        self.clock.now = 125
        self.cycle.post_connect_dwell_complete = True
        self.device.window = STORE
        self.assertTrue(self.cycle.return_from_ad_landing(self.cycle.observe()))
        self.assertEqual(self.cycle.observed_post_connect_ad['at'], at)
        self.assertEqual(self.taps(), [('native_tap',42,345)])

    def test_missing_fresh_navigation_is_not_replaced_with_adb_or_coordinates(self):
        self.completed_dwell()
        current = self.cycle.observe(); current['navigation_controls'] = []
        self.assertFalse(self.cycle.return_from_ad_landing(current))
        self.assertFalse(self.taps()); self.assertEqual(self.cycle.ad_landing_returns, 0)

    def test_returned_origin_without_close_uses_only_one_observed_back(self):
        origin = copy.deepcopy(W_AD); origin['controls'] = []
        self.device.window = origin
        self.cycle.post_connect_dwell_active = True
        self.cycle.observe_post_connect_ad(self.cycle.observe())
        self.cycle.post_connect_dwell_complete = True
        self.device.window = STORE; self.device.on_back = origin
        self.cycle.dismiss_ads()
        self.assertEqual(self.taps(), [('native_tap',42,345),('native_tap',42,345)])
        self.assertEqual(self.device.window, MAIN)

    def test_store_that_does_not_leave_fails_after_one_back(self):
        self.completed_dwell()
        self.device.on_back = STORE
        with self.assertRaisesRegex(RuntimeError, 'return limit exhausted'):
            self.cycle.dismiss_ads()
        self.assertEqual(self.taps(), [('native_tap',42,345)])


if __name__ == '__main__': unittest.main()
