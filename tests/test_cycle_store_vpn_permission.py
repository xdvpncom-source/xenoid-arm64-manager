"""A configured store cannot hide the target package's granted VPN permission."""
import copy
import unittest
from unittest.mock import patch

from test_cycle_ad_windows import Clock, MAIN, Replay, STORE, m


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = Replay(copy.deepcopy(STORE), on_back=MAIN)
        self.cycle = m.Cycle(self.device, "v2.vpn.fast")
        self.permission = "allow"
        self.appops_queries = []
        for item in (patch.object(m, "time", self.clock),
                     patch.object(m, "VPN_READY_SECONDS", 6),
                     patch.object(m, "AD_READY_SECONDS", 16)):
            item.start(); self.addCleanup(item.stop)

        def adb(args):
            if args == m.CRASH_FOCUS_COMMAND:
                return "mCurrentFocus=Window{1 u0 " + self.device.window["component"] + "}"
            expected = ["shell", "appops", "get", "--user", "0",
                        "v2.vpn.fast", "ACTIVATE_VPN"]
            self.assertEqual(args, expected)
            self.appops_queries.append(list(args))
            return "ACTIVATE_VPN: " + self.permission + "; time=+2m ago"
        self.device.adb = adb

    def taps(self):
        return [row for row in self.device.actions if row[0] == "native_tap"]

    def test_configured_store_accepts_exact_target_permission_without_input(self):
        self.cycle.approve_vpn(ads=False)
        self.assertEqual(self.clock.now, 0)
        self.assertEqual(len(self.appops_queries), 1)
        self.assertFalse(self.taps())

    def test_configured_store_with_target_permission_denied_keeps_waiting(self):
        self.permission = "deny"
        with self.assertRaisesRegex(RuntimeError, "VPN permission was not confirmed"):
            self.cycle.approve_vpn(ads=False)
        self.assertGreaterEqual(self.clock.now, 6)
        self.assertGreater(len(self.appops_queries), 1)
        self.assertFalse(self.taps())

    def test_unconfigured_foreign_window_is_not_accepted(self):
        self.device.window.update(package="unrelated.browser",
                                  component="unrelated.browser/MainActivity")
        with self.assertRaisesRegex(RuntimeError, "VPN permission was not confirmed"):
            self.cycle.approve_vpn(ads=False)
        self.assertFalse(self.appops_queries)
        self.assertFalse(self.taps())

    def test_ads_enabled_keeps_existing_store_policy(self):
        with self.assertRaisesRegex(RuntimeError, "VPN permission was not confirmed"):
            self.cycle.approve_vpn(ads=True)
        self.assertFalse(self.appops_queries)
        self.assertFalse(self.taps())

    def test_store_must_be_explicitly_configured(self):
        with patch.object(m, "AD_LANDING_PACKAGES", ()), \
                self.assertRaisesRegex(RuntimeError, "VPN permission was not confirmed"):
            self.cycle.approve_vpn(ads=False)
        self.assertFalse(self.appops_queries)
        self.assertFalse(self.taps())

    def test_permission_does_not_skip_full_dwell_or_send_early_back(self):
        self.cycle.approve_vpn(ads=False)
        self.cycle.post_connect_dwell_active = True
        self.cycle.wait(120, dialogs=True, vpn=True, ads=False)
        self.assertEqual(self.clock.now, 120)
        self.assertFalse(self.taps())
        self.assertFalse(self.cycle.return_from_ad_landing(self.cycle.observe()))
        self.cycle.post_connect_dwell_active = False
        self.cycle.post_connect_dwell_complete = True
        self.cycle.dismiss_ads()
        self.assertEqual(self.taps(), [("native_tap", 42, 345)])
        self.assertEqual(self.device.window, MAIN)


if __name__ == "__main__":
    unittest.main()
