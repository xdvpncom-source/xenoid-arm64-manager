"""The assigned-app workflow waits to close the main ad until VPN permission."""
import copy
import unittest
from unittest.mock import patch
from test_vpn_cycle import Clock, FakeDevice, control, m, window


def consent_control(text, box):
    return {**control(text, box=box), 'window_id': 5, 'display_id': 0,
            'window_active': True, 'window_focused': True}


class PostConnectDevice(FakeDevice):
    def __init__(self, clock):
        super().__init__(clock)
        self.approved_at = None
        self.ad_closed_at = None
        self.late_popup_done = False

    def app_open(self):
        super().app_open()
        self.state = 'notification'

    def inspect_window(self, **kwargs):
        self.trace.append(('inspect', self.clock.now, self.state))
        if self.state == 'notification':
            return window('GrantPermissionsActivity', [control('Allow app to send notifications?'),
                control('Allow', 'com.android.permissioncontroller:id/permission_allow_button')],
                package='com.android.permissioncontroller')
        if self.state == 'ump':
            return window('MainActivity', [consent_control('App asks for your consent to use your personal data', (0,0,900,100)),
                consent_control('Consent', (0,200,250,250)), consent_control('Do not consent', (300,200,650,250)),
                consent_control('Manage options', (0,300,350,350))])
        if self.state == 'terms':
            return window('MainActivity', [control('Data collection disclosure'), control('Close', box=(900,60,940,100))])
        if self.state == 'main':
            return window('MainActivity', [control('Connect', box=(400,400,600,600))])
        if self.state == 'vpn':
            return window('ConfirmDialog', [control('Connection request'), control('OK', 'android:id/button1')],
                          package='com.android.vpndialogs')
        if self.state == 'connected_ad':
            return window('com.google.android.gms.ads.AdActivity', [control('Advertisement', box=(0,0,1000,1000)),
                control('Interstitial close button', box=(900,10,940,50))])
        if (self.ad_closed_at is not None and not self.late_popup_done
                and self.clock.now >= self.ad_closed_at + 5):
            self.state = 'late_popup'
            return window('MainActivity', [control('Close', box=(900,60,940,100))])
        return window('MainActivity', [control('Connected', box=(400,400,600,600))])

    def tap(self, x, y):
        previous = self.state
        self.trace.append(('tap', self.clock.now, previous, x, y))
        if previous == 'notification': self.state = 'ump'
        elif previous == 'ump':
            if not (300 <= x <= 650 and 200 <= y <= 250):
                raise AssertionError('The observed Do not consent control was not selected')
            self.state = 'terms'
        elif previous == 'terms': self.state = 'main'
        elif previous == 'main': self.state = 'vpn'
        elif previous == 'vpn':
            self.state = 'connected_ad'; self.allowed = True; self.approved_at = self.clock.now
        elif previous == 'connected_ad': self.state = 'holding'; self.ad_closed_at = self.clock.now
        elif previous == 'late_popup': self.state = 'holding'; self.late_popup_done = True
        else: raise AssertionError('Unexpected input to ' + previous)


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = PostConnectDevice(self.clock)
        self.cycle = m.Cycle(self.device, 'qa.vpn')
        self.patches = [patch.object(m, 'time', self.clock), patch.object(m, 'REGENERATE_ENABLED', False)]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def test_complete_order_immediate_startup_then_post_vpn_dwell_then_ad_and_hold(self):
        with patch.object(m.random, 'randint', return_value=97): self.cycle.run()
        taps = [row for row in self.device.trace if row[0] == 'tap']
        self.assertEqual([row[2] for row in taps],
                         ['notification','ump','terms','main','vpn','connected_ad','late_popup'])
        self.assertEqual(taps[0][1], 0)
        self.assertLess(self.device.approved_at, 90, 'No unconditional startup ad dwell')
        self.assertGreaterEqual(self.device.ad_closed_at - self.device.approved_at, 97)
        self.assertLess(self.device.ad_closed_at - self.device.approved_at, 105)
        final_close = [row for row in self.device.trace if row[0] == 'close'][-1]
        self.assertGreaterEqual(final_close[1] - self.device.ad_closed_at, 10)
        clears = [row for row in self.device.trace if row[0] == 'clear']
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0][2:], ('qa.vpn', '0'))
        self.assertGreater(clears[0][1], taps[-1][1])
        self.assertFalse(any(row[0] == 'adb' and row[2][:2] == ['shell','input'] for row in self.device.trace))

    def test_permission_confirmation_preserves_already_authorized_foreground_ad(self):
        self.device.state = 'connected_ad'; self.device.allowed = True
        self.cycle.approve_vpn(ads=False)
        self.assertFalse(any(row[0] == 'tap' for row in self.device.trace))
        self.assertEqual(self.clock.now, 0)

    def test_permission_stage_does_not_dismiss_ad_to_manufacture_permission(self):
        self.device.state = 'connected_ad'
        with patch.object(m, 'VPN_READY_SECONDS', 6), self.assertRaisesRegex(RuntimeError, 'VPN permission'):
            self.cycle.approve_vpn(ads=False)
        self.assertFalse(any(row[0] == 'tap' for row in self.device.trace))

    def test_startup_preserves_nonblocking_banner_with_connect_visible(self):
        self.device.state = 'main'
        original = window('MainActivity', [control('Connect', box=(400,400,600,600)),
            control('Advertisement', box=(0,900,1000,1000)), control('Close', box=(950,900,990,940))])
        self.device.inspect_window = lambda **kw: copy.deepcopy(original)
        ready = self.cycle.prepare_screen(10, blocking_ads_only=True)
        self.assertEqual(ready, original)
        self.assertFalse(any(row[0] == 'tap' for row in self.device.trace))

    def test_main_ad_is_not_closed_by_post_permission_wait(self):
        self.device.state = 'connected_ad'; self.device.allowed = True
        self.cycle.wait(95, dialogs=True, vpn=True, ads=False)
        self.assertEqual(self.clock.now, 95)
        self.assertFalse(any(row[0] == 'tap' for row in self.device.trace))

    def test_late_notification_is_allowed_during_dwell_without_closing_main_ad(self):
        self.device.state = 'notification'; self.device.allowed = True
        def approve(x, y):
            self.device.trace.append(('tap', self.clock.now, self.device.state, x, y))
            self.device.state = 'connected_ad'
        self.device.tap = approve
        self.cycle.wait(20, dialogs=True, vpn=True, ads=False)
        self.assertEqual([row[2] for row in self.device.trace if row[0] == 'tap'], ['notification'])
        self.assertEqual(self.clock.now, 20)

    def test_failed_permission_has_no_random_dwell_or_regeneration(self):
        with patch.object(self.cycle, 'approve_vpn', side_effect=RuntimeError('permission pending')), \
                patch.object(m.random, 'randint') as draw, \
                patch.object(self.cycle, 'regenerate_after_success') as regenerate:
            with self.assertRaisesRegex(RuntimeError, 'permission pending'):
                self.cycle.run()
        draw.assert_not_called(); regenerate.assert_not_called()
        self.assertTrue(any(row[0] == 'clear' for row in self.device.trace))

    def test_successful_cleanup_precedes_unchanged_regeneration_stage(self):
        with patch.object(m.random, 'randint', return_value=90), patch.object(self.cycle, 'regenerate_after_success') as regenerate:
            self.cycle.run()
        regenerate.assert_called_once_with()
        self.assertTrue(any(row[0] == 'clear' for row in self.device.trace))


if __name__ == '__main__': unittest.main()
