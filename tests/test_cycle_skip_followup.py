"""Skip advances an ad; only a fresh Close or verified clear app finishes it."""
import copy
import unittest
from unittest.mock import patch

from test_cycle_ad_windows import Clock, MAIN, Replay, STORE, W_AD, ad, control, m


SKIP = {**control('Skip', (120, 25, 160, 55)), 'resource_id': 'qa:id/skip_ad'}
CLOSE = control('Close', (178, 0, 200, 20))
SKIP_POINT = (140, 40)
CLOSE_POINT = (189, 10)
BACK_POINT = (42, 345)


def ad_page(*buttons):
    value = ad(copy.deepcopy(buttons[0]))
    value['controls'] = value['controls'][:1] + copy.deepcopy(list(buttons))
    value['display_id'] = 0
    value['navigation_controls'] = copy.deepcopy(W_AD['navigation_controls'])
    return value


class FollowupDevice(Replay):
    def __init__(self, clock, *, both=False, store=False, unchanged_for=0):
        super().__init__(ad_page(SKIP, CLOSE) if both else ad_page(SKIP))
        self.clock = clock
        self.phase = 'both' if both else 'skip'
        self.store_after_skip = store
        self.unchanged_for = unchanged_for
        self.skip_at = None
        self.timeline = []
        self.read_count = 0

    def inspect_window(self, **kwargs):
        self.read_count += 1
        if self.read_count > 80:
            raise AssertionError('Ad follow-up kept reading without bounded progress')
        if (self.phase == 'skip' and self.skip_at is not None
                and self.clock.now >= self.skip_at + self.unchanged_for):
            self.phase = 'endcard'
            self.window = ad_page(CLOSE)
        self.timeline.append(('read', self.clock.now, self.phase))
        return super().inspect_window(**kwargs)

    def tap(self, x, y):
        point = (x, y)
        self.actions.append(('native_tap', x, y))
        self.timeline.append(('tap', self.clock.now, self.phase, point))
        if self.phase in ('skip', 'both') and point == SKIP_POINT:
            if self.skip_at is None:
                self.skip_at = self.clock.now
            if self.store_after_skip:
                self.phase = 'store'
                self.window = copy.deepcopy(STORE)
            elif not self.unchanged_for:
                self.phase = 'endcard'
                self.window = ad_page(CLOSE)
        elif self.phase in ('both', 'endcard') and point == CLOSE_POINT:
            self.phase = 'clear'
            self.window = copy.deepcopy(MAIN)
        elif self.phase == 'store' and point == BACK_POINT:
            self.phase = 'endcard'
            self.window = ad_page(CLOSE)
        else:
            raise AssertionError('Unexpected ad action %r in %s' % (point, self.phase))

    def log(self, message):
        super().log(message)
        self.timeline.append(('log', self.clock.now, self.phase, message))


class SkipFollowupTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for item in (patch.object(m, 'time', self.clock),
                     patch.object(m, 'AD_READY_SECONDS', 30)):
            item.start(); self.addCleanup(item.stop)

    def run_ad(self, **options):
        device = FollowupDevice(self.clock, **options)
        cycle = m.Cycle(device, 'v2.vpn.fast')
        # This test starts at the real post-permission/dwell ad-handling phase.
        cycle.post_connect_dwell_complete = True
        cycle.dismiss_ads()
        return device

    @staticmethod
    def taps(device):
        return [event[3] for event in device.timeline if event[0] == 'tap']

    def assert_fresh_read_between(self, device, first_point, next_point):
        first = next(i for i, row in enumerate(device.timeline)
                     if row[0] == 'tap' and row[3] == first_point)
        second = next(i for i, row in enumerate(device.timeline)
                      if i > first and row[0] == 'tap' and row[3] == next_point)
        self.assertTrue(any(row[0] == 'read' for row in device.timeline[first+1:second]),
                        'A new hierarchy must be read between these inputs')

    def assert_clean_app_verified(self, device):
        close = max(i for i, row in enumerate(device.timeline)
                    if row[0] == 'tap' and row[3] == CLOSE_POINT)
        clean_reads = [row for row in device.timeline[close+1:]
                       if row[0] == 'read' and row[2] == 'clear']
        self.assertGreaterEqual(len(clean_reads), 2)
        self.assertGreaterEqual(clean_reads[-1][1] - clean_reads[0][1], m.POST_ACTION_QUIET_SECONDS)
        self.assertEqual(device.phase, 'clear')

    def test_real_close_label_beats_skip_id_when_both_are_visible(self):
        device = self.run_ad(both=True)
        self.assertEqual(self.taps(device), [CLOSE_POINT])
        self.assert_clean_app_verified(device)

    def test_skip_then_fresh_endcard_close_are_distinct_steps(self):
        device = self.run_ad()
        self.assertEqual(self.taps(device), [SKIP_POINT, CLOSE_POINT])
        self.assert_fresh_read_between(device, SKIP_POINT, CLOSE_POINT)
        inputs = [row for row in device.timeline
                  if row[0] == 'log' and row[3].startswith('Input sent:')]
        self.assertEqual(len(inputs), 2)
        self.assertIn('skip', inputs[0][3].casefold())
        self.assertIn('close', inputs[1][3].casefold())
        skip_log = device.timeline.index(inputs[0])
        close_log = device.timeline.index(inputs[1])
        self.assertTrue(any(row[0] == 'log' and 'skip' in row[3].casefold()
                            and ('fresh' in row[3].casefold() or 'reinspect' in row[3].casefold())
                            for row in device.timeline[skip_log+1:close_log]),
                        'Log that the next decision uses a fresh post-Skip observation')
        self.assert_clean_app_verified(device)

    def test_skip_store_return_uses_one_official_back_then_real_close(self):
        device = self.run_ad(store=True)
        self.assertEqual(self.taps(device), [SKIP_POINT, BACK_POINT, CLOSE_POINT])
        self.assert_fresh_read_between(device, SKIP_POINT, BACK_POINT)
        self.assert_fresh_read_between(device, BACK_POINT, CLOSE_POINT)
        self.assert_clean_app_verified(device)

    def test_unchanged_skip_is_tapped_once_while_waiting_for_close(self):
        device = self.run_ad(unchanged_for=6)
        self.assertEqual(self.taps(device), [SKIP_POINT, CLOSE_POINT])
        waiting = [row for row in device.timeline if row[0] == 'read' and row[2] == 'skip']
        self.assertGreaterEqual(len(waiting), 2)
        close = next(row for row in device.timeline if row[0] == 'tap' and row[3] == CLOSE_POINT)
        self.assertGreaterEqual(close[1], 6)
        self.assert_fresh_read_between(device, SKIP_POINT, CLOSE_POINT)
        self.assert_clean_app_verified(device)


if __name__ == '__main__':
    unittest.main()
