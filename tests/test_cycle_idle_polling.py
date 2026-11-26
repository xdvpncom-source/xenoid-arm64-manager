"""Idle polling reduces full dumps without hiding same-window dialogs or inputs."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.modules.setdefault('xenoid_device', types.SimpleNamespace(Device=object))
spec = importlib.util.spec_from_file_location('idle_poll_cycle', Path(__file__).resolve().parents[1]/'qa/vpn-cycle.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Clock:
    def __init__(self): self.now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds):
        if seconds < 0: raise AssertionError('negative sleep')
        self.now += seconds


def control(text='', resource_id='', box=(10, 10, 30, 30)):
    return {'text': text, 'content_desc': '', 'resource_id': resource_id,
            'class_name': 'android.widget.Button', 'package': 'qa.vpn',
            'bounds': list(box), 'enabled': True, 'clickable': True}


def window(items=None, package='qa.vpn', component='qa.vpn/.MainActivity'):
    return {'stable': True, 'package': package, 'component': component,
            'controls': items if items is not None else [control('Connect')]}


class Device:
    def __init__(self, clock):
        self.clock = clock
        self.ui_reads = []
        self.focus_reads = []
        self.taps = []
        self.messages = []
        self.focus = lambda: 'mCurrentFocus=Window{1 u0 qa.vpn/.MainActivity}'
        self.render = lambda: window()
        self.on_tap = lambda: None
    def settings(self): return {'android_user': 0}
    def log(self, message): self.messages.append(message)
    def adb(self, args):
        if args != m.CRASH_FOCUS_COMMAND:
            raise AssertionError('No ADB input or unexpected command: '+str(args))
        self.focus_reads.append(self.clock.now)
        return self.focus()
    def inspect_window(self, *, priority=None, windows=False):
        self.ui_reads.append((self.clock.now, priority))
        return self.render()
    def tap(self, x, y):
        self.taps.append((self.clock.now, x, y))
        self.on_tap()
    def set_frame_rate(self, *args, **kwargs): return {'changed': False}
    def apply_resource_defaults(self, timeout=None): return {'ok': True}


class IdlePollingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.device = Device(self.clock)
        self.cycle = m.Cycle(self.device, 'qa.vpn')
        for name, value in [('time', self.clock), ('TAP_BACKEND', 'native'),
                            ('IDLE_FOCUS_POLL_SECONDS', 5), ('IDLE_UI_MIN_SECONDS', 2),
                            ('IDLE_UI_MAX_SECONDS', 10)]:
            handle = patch.object(m, name, value)
            handle.start()
            self.addCleanup(handle.stop)

    def test_unobstructed_100s_keeps_periodic_reads_but_not_fifty_full_dumps(self):
        self.cycle.wait(100, dialogs=True, vpn=True, ads=True)
        times = [r[0] for r in self.device.ui_reads]
        self.assertEqual(self.clock.now, 100)
        self.assertGreaterEqual(len(times), 2)
        self.assertLessEqual(len(times), 14)
        self.assertLessEqual(max(b-a for a, b in zip(times, times[1:])), 10)
        self.assertTrue(all(priority == 0 for _, priority in self.device.ui_reads))
        self.assertFalse(self.device.taps)

    def test_passive_100s_reads_stable_ui_once_but_keeps_cheap_focus_checks(self):
        self.cycle.wait(100, dialogs=True, vpn=True, ads=False, periodic_ui=False)
        self.assertEqual(self.clock.now, 100)
        self.assertEqual(self.device.ui_reads, [(0, 0)])
        self.assertEqual(self.device.focus_reads, list(range(0, 100, 5)))
        self.assertFalse(self.device.taps)

    def test_passive_focus_change_still_forces_a_fresh_permission_snapshot(self):
        self.assertIsNotNone(self.cycle.observe_idle(periodic_ui=False))
        self.clock.now = 12
        self.assertIsNone(self.cycle.observe_idle(periodic_ui=False))
        self.device.focus = lambda: 'mCurrentFocus=Window{2 u0 com.android.vpndialogs/.ConfirmDialog}'
        self.device.render = lambda: window([control('OK', 'android:id/button1')],
                                            package=m.VPN_PACKAGE, component=m.VPN_PACKAGE+'/.ConfirmDialog')
        snapshot = self.cycle.observe_idle(periodic_ui=False)
        self.assertIsNotNone(snapshot)
        self.assertTrue(self.cycle.service_dialog(snapshot, vpn=True))
        self.assertEqual(self.device.ui_reads, [(0, 0), (12, 0)])
        self.assertEqual(len(self.device.taps), 1)

    def test_passive_input_still_requires_a_fresh_ui_read(self):
        self.assertIsNotNone(self.cycle.observe_idle(periodic_ui=False))
        self.clock.now = 1
        self.assertIsNone(self.cycle.observe_idle(periodic_ui=False))
        self.cycle.tap_point(50, 50, 'test native action')
        self.assertIsNotNone(self.cycle.observe_idle(periodic_ui=False))
        self.assertEqual(self.device.ui_reads, [(0, 0), (1, 0)])
        self.assertEqual(len(self.device.taps), 1)

    def test_passive_unstable_read_is_retried_without_enabling_periodic_dumps(self):
        self.device.render = lambda: dict(window(), stable=False)
        self.assertIsNone(self.cycle.observe_idle(periodic_ui=False))
        self.clock.now = m.IDLE_UI_MIN_SECONDS
        self.device.render = lambda: window()
        self.assertIsNotNone(self.cycle.observe_idle(periodic_ui=False))
        self.clock.now = 50
        self.assertIsNone(self.cycle.observe_idle(periodic_ui=False))
        self.assertEqual(self.device.ui_reads, [(0, 0), (m.IDLE_UI_MIN_SECONDS, 0)])

    def test_same_focus_same_component_ad_is_seen_by_periodic_full_read(self):
        shown_at = 17
        closed = [False]
        def render():
            if self.clock.now >= shown_at and not closed[0]:
                return window([control('Advertisement', box=(0, 0, 200, 356)),
                               control('Close', box=(170, 10, 190, 30))])
            return window()
        self.device.render = render
        self.device.on_tap = lambda: closed.__setitem__(0, True)
        self.cycle.wait(40, dialogs=True, ads=True)
        self.assertTrue(closed[0])
        self.assertEqual(len(self.device.taps), 1)
        tapped_at = self.device.taps[0][0]
        self.assertGreaterEqual(tapped_at, shown_at)
        self.assertLessEqual(tapped_at-shown_at, m.IDLE_UI_MAX_SECONDS)
        # A new full snapshot is acquired immediately after the action.
        self.assertGreaterEqual(sum(t == tapped_at for t, _ in self.device.ui_reads), 2)

    def test_changed_focus_forces_full_read_before_periodic_due(self):
        self.cycle.observe_idle()
        self.clock.now = 1
        self.device.focus = lambda: 'mCurrentFocus=Window{2 u0 com.android.vpndialogs/.ConfirmDialog}'
        self.device.render = lambda: window([control('OK', 'android:id/button1')],
                                            package=m.VPN_PACKAGE, component=m.VPN_PACKAGE+'/.ConfirmDialog')
        snapshot = self.cycle.observe_idle()
        self.assertIsNotNone(snapshot)
        self.assertEqual([t for t, _ in self.device.ui_reads], [0, 1])
        self.assertTrue(self.cycle.service_dialog(snapshot, vpn=True))
        self.assertEqual(len(self.device.taps), 1)

    def test_bluetooth_crash_hint_forces_fresh_read_and_native_close(self):
        self.cycle.observe_idle()
        self.clock.now = 1
        self.device.focus = lambda: 'mCurrentFocus=Window{3 u0 Application Error: com.android.bluetooth}'
        self.device.render = lambda: window([control('Bluetooth keeps stopping'),
                    control('Close app', 'android:id/aerr_close')], package='android',
                    component='Application Error: com.android.bluetooth')
        snapshot = self.cycle.observe_idle()
        self.assertTrue(self.cycle.service_dialog(snapshot, vpn=True, ads=True))
        self.assertEqual(len(self.device.taps), 1)
        self.assertIsNone(self.cycle.target_crash)

    def test_target_crash_is_not_masked_by_backoff(self):
        self.device.focus = lambda: 'mCurrentFocus=Window{3 u0 Application Error: qa.vpn}'
        self.device.render = lambda: window([control('Close app', 'android:id/aerr_close')],
                                            package='android', component='Application Error: qa.vpn')
        with self.assertRaisesRegex(RuntimeError, 'assigned app crashed'):
            self.cycle.wait(10, dialogs=True, ads=True)

    def test_retryable_crash_input_failure_still_waits_instead_of_busy_spinning(self):
        self.device.focus = lambda: 'mCurrentFocus=Window{3 u0 Application Error: com.android.bluetooth}'
        self.device.render = lambda: window([control('Close app', 'android:id/aerr_close')],
                                            package='android', component='Application Error: com.android.bluetooth')
        self.device.tap = Mock(side_effect=RuntimeError('input not ready'))
        old_sleep = self.clock.sleep
        sleeps = []
        def sleep(seconds):
            self.assertGreater(seconds, 0)
            sleeps.append(seconds)
            old_sleep(seconds)
        self.clock.sleep = sleep
        self.cycle.wait(5, dialogs=True, ads=True)
        self.assertEqual(self.clock.now, 5)
        self.assertTrue(sleeps)
        self.assertLessEqual(len(self.device.ui_reads), 3)

    def test_unchanged_modal_cannot_receive_two_taps_inside_cooldown(self):
        self.device.render = lambda: window([control('Advertisement', box=(0, 0, 200, 356)),
                                            control('Close', box=(170, 10, 190, 30))])
        self.cycle.wait(3, dialogs=True, ads=True)
        self.assertEqual([t[0] for t in self.device.taps], [0, 2])
        self.assertGreaterEqual(len(self.device.ui_reads), 4)

    def test_external_cycle_tap_invalidates_due_without_reusing_controls(self):
        self.assertIsNotNone(self.cycle.observe_idle())
        self.clock.now = 1
        self.assertIsNone(self.cycle.observe_idle())
        self.cycle.tap_point(50, 50, 'test native action')
        self.assertIsNotNone(self.cycle.observe_idle())
        self.assertEqual([t for t, _ in self.device.ui_reads], [0, 1])
        self.assertEqual(len(self.device.taps), 1)

    def test_failed_input_also_discards_idle_observation(self):
        self.cycle.observe_idle()
        self.clock.now = 1
        self.device.tap = Mock(side_effect=RuntimeError('reply lost'))
        with self.assertRaisesRegex(RuntimeError, 'reply lost'):
            self.cycle.tap_point(50, 50, 'test native action')
        self.assertIsNotNone(self.cycle.observe_idle())

    def test_unreadable_focus_or_unstable_ui_never_reuses_previous_snapshot(self):
        self.cycle.observe_idle()
        self.clock.now = 1
        self.device.focus = Mock(side_effect=RuntimeError('temporary transport loss'))
        self.device.render = lambda: dict(window(), stable=False)
        self.assertIsNone(self.cycle.observe_idle())
        self.assertEqual(len(self.device.ui_reads), 2)

    def test_regeneration_invalidates_prior_idle_state(self):
        self.cycle.observe_idle()
        self.clock.now = 1
        result = dict.fromkeys(('ok', 'native_ready', 'profile_ready', 'container_id_preserved',
            'instance_id_preserved', 'resource_tag_preserved', 'identities_changed',
            'input_ready', 'foreground_ready'), True)
        result.update(phase='committed', runtimeOnly=True, containerRecreated=False)
        self.device.regenerate = Mock(return_value=result)
        invalidate = Mock(wraps=self.cycle.invalidate_idle_monitor)
        self.cycle.invalidate_idle_monitor = invalidate
        with patch.object(m.random, 'randint', return_value=5), patch.object(m, 'GOOGLE_CACHE_PACKAGES', ()), \
                patch.object(m, 'GOOGLE_DATA_RESET_ENABLED', False), patch.object(m, 'UI_PREFERENCES', None), \
                patch.object(m, 'configure_display', return_value={}):
            self.assertTrue(self.cycle.regenerate_after_success())
        self.assertGreaterEqual(invalidate.call_count, 2)
        self.assertIsNotNone(self.cycle.observe_idle())

    def test_custom_periodic_bound_is_honored_even_when_focus_cadence_is_larger(self):
        with patch.object(m, 'IDLE_UI_MAX_SECONDS', 3), patch.object(m, 'IDLE_FOCUS_POLL_SECONDS', 9):
            self.cycle.wait(14, dialogs=True)
        times = [r[0] for r in self.device.ui_reads]
        self.assertLessEqual(max(b-a for a, b in zip(times, times[1:])), 3)

    def test_invalid_editable_intervals_fail_before_any_input(self):
        for changes in ({'IDLE_UI_MAX_SECONDS': 1}, {'IDLE_UI_MIN_SECONDS': 0},
                        {'IDLE_FOCUS_POLL_SECONDS': float('nan')}):
            with self.subTest(changes=changes), patch.multiple(m, **changes):
                with self.assertRaisesRegex(ValueError, 'idle polling intervals'):
                    self.cycle.wait(10, dialogs=True)
        self.assertFalse(self.device.taps)
        self.assertFalse(self.device.ui_reads)

if __name__ == '__main__': unittest.main()
