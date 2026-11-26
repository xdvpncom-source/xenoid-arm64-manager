"""Collapse only the observed Android shade, without clicking notifications."""
import copy
import unittest
from unittest.mock import patch

from test_cycle_ad_windows import Clock, MAIN, Replay, STORE, control, fixture, m


SHADE = {**copy.deepcopy(STORE), **fixture("actual-systemui-notification-shade.json"),
         "controls": [{**control("Dismiss all", (140,280,195,320)),
                       "package": "com.android.systemui",
                       "resource_id": "com.android.systemui:id/dismiss_text"}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = Replay(copy.deepcopy(SHADE))
        self.cycle = m.Cycle(self.device, "v2.vpn.fast")
        item = patch.object(m, "time", self.clock)
        item.start(); self.addCleanup(item.stop)
        def tap(x, y):
            self.assertEqual((x, y), (42,345), "Never click a notification or Dismiss all")
            self.device.actions.append(("native_tap", x, y))
            self.device.window = copy.deepcopy(MAIN)
        self.device.tap = tap
        def adb(args):
            if args == m.CRASH_FOCUS_COMMAND:
                return "mCurrentFocus=Window{1 u0 " + self.device.window["component"] + "}"
            self.assertEqual(args, ["shell","appops","get","--user","0","v2.vpn.fast","ACTIVATE_VPN"])
            self.assertEqual(self.device.window["package"], "v2.vpn.fast")
            return "ACTIVATE_VPN: allow"
        self.device.adb = adb

    def taps(self):
        return [row for row in self.device.actions if row[0] == "native_tap"]

    def test_exact_shade_has_one_official_back_then_fresh_app_observation(self):
        self.assertEqual(self.device.window["component"], "NotificationShade")
        self.assertTrue(self.cycle.service_dialog(self.cycle.observe()))
        self.assertEqual(self.taps(), [("native_tap",42,345)])
        fresh = self.cycle.observe()
        self.assertEqual(fresh["package"], "v2.vpn.fast")
        self.assertFalse(self.cycle.service_dialog(fresh))
        self.assertEqual(len(self.taps()), 1)

    def test_fully_qualified_shade_component_is_also_recognized(self):
        self.device.window["component"] = "com.android.systemui/NotificationShade"
        self.assertTrue(self.cycle.service_dialog(self.cycle.observe()))
        self.assertEqual(self.taps(), [("native_tap",42,345)])

    def test_prepare_waits_for_fresh_app_and_never_resolves_shade_as_home(self):
        with patch.object(self.cycle, "is_home_window", side_effect=AssertionError("shade is not HOME")):
            fresh = self.cycle.prepare_screen(12)
        self.assertEqual(fresh["package"], "v2.vpn.fast")
        self.assertGreaterEqual(self.clock.now, m.POST_ACTION_QUIET_SECONDS)
        index = self.device.actions.index(("native_tap",42,345))
        self.assertGreaterEqual(self.device.actions[index+1:].count(("read",)), 2)

    def test_dismiss_ads_collapses_shade_before_generic_close_selection(self):
        original = self.cycle.ad_close_control
        def select(window, *args, **kwargs):
            self.assertFalse(m.is_notification_shade(window))
            return original(window, *args, **kwargs)
        with patch.object(self.cycle, "ad_close_control", side_effect=select):
            self.cycle.dismiss_ads()
        self.assertEqual(self.taps(), [("native_tap",42,345)])

    def test_crash_notification_card_never_triggers_its_close_control(self):
        self.device.window["controls"].append({**control("Bluetooth keeps stopping"),
                                               "resource_id": "android:id/aerr_close"})
        with patch.object(self.cycle, "close_crash_dialog", side_effect=AssertionError("notification is not a crash overlay")):
            self.assertTrue(self.cycle.service_dialog(self.cycle.observe()))
        self.assertEqual(self.taps(), [("native_tap",42,345)])

    def test_idle_wait_handles_shade_and_keeps_full_wait(self):
        self.cycle.wait(10, dialogs=True, vpn=True, ads=False)
        self.assertEqual(self.clock.now, 10)
        self.assertEqual(self.taps(), [("native_tap",42,345)])

    def test_permission_is_confirmed_only_after_shade_back_and_fresh_app(self):
        self.cycle.approve_vpn(ads=False)
        self.assertEqual(self.taps(), [("native_tap",42,345)])
        self.assertGreaterEqual(self.clock.now, m.POLL_SECONDS)

    def test_other_systemui_and_foreign_shades_are_not_collapsed(self):
        for package, component in (
                ("com.android.systemui", "com.android.systemui/VolumeDialog"),
                ("com.android.systemui", "com.android.systemui/OtherNotificationShade"),
                ("com.android.permissioncontroller", "com.android.systemui/NotificationShade"),
                ("unconfigured.app", "unconfigured.app/NotificationShade")):
            self.device.window = {**copy.deepcopy(SHADE), "package": package, "component": component}
            self.assertFalse(self.cycle.close_notification_shade(self.cycle.observe()))
        self.assertFalse(self.taps())

    def test_action_cooldown_prevents_immediate_second_back(self):
        self.assertTrue(self.cycle.close_notification_shade(self.cycle.observe()))
        self.device.window = copy.deepcopy(SHADE)
        self.assertFalse(self.cycle.close_notification_shade(self.cycle.observe()))
        self.assertEqual(len(self.taps()), 1)

    def test_expired_snapshot_is_never_used_for_back(self):
        current = self.cycle.observe()
        self.clock.now += m.AD_NAV_SNAPSHOT_MAX_SECONDS + 1
        self.assertFalse(self.cycle.close_notification_shade(current))
        self.assertFalse(self.taps())

    def test_unproven_focus_and_missing_back_never_use_alternative_input(self):
        current = self.cycle.observe(); current["focus_observation"]["exact_match"] = False
        self.assertFalse(self.cycle.close_notification_shade(current))
        current = self.cycle.observe(); current["navigation_controls"] = []
        self.assertFalse(self.cycle.close_notification_shade(current))
        self.assertFalse(self.taps())

    def test_rejected_navigation_does_not_click_dismiss_all(self):
        self.device.reject_navigation = True
        with patch.object(m, "AD_READY_SECONDS", 6), \
                self.assertRaisesRegex(RuntimeError, "usable close control"):
            self.cycle.dismiss_ads()
        self.assertFalse(self.taps())


if __name__ == "__main__":
    unittest.main()
