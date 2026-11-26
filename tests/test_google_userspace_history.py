"""Run the actual native history method against soft-reboot clock histories."""
from __future__ import annotations
import ast
from datetime import datetime, timezone
from pathlib import Path
import types
import unittest

from test_proxy_observer_classification import patched_observer

SOURCE = 'shipped-native-backend-patch'
tree = ast.parse(patched_observer())
owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RuntimeManager')
method = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == '_microg_exit_history')
namespace = {'Any': object, 'time': types.SimpleNamespace(monotonic=lambda: 0)}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), 'exec'), namespace)


class HistoryTests(unittest.TestCase):
    def fixture(self, records=(), identities=((10, 900000), (10, 900000)), outputs=None):
        responses = {('dumpsys', 'activity', 'exit-info'): 'retained history',
                     ('getconf', 'CLK_TCK'): '100', ('cat', '/proc/uptime'): '10000.0 5000.0',
                     ('date', '+%s'): '2000000000', ('date', '+%z'): '+0000'}
        responses.update(outputs or {})
        ids = iter(identities)
        b = types.SimpleNamespace(_adb_shell_text=lambda args, **kw: responses[tuple(args)],
            _microg_process_identity=lambda name: next(ids), _parse_exit_info=lambda *_: list(records),
            _parse_exit_timestamp=lambda value, zone: float(value),
            _EXIT_REASON_BY_CODE={1:'OTHER', 4:'CRASH', 6:'ANR', 7:'INITIALIZATION_FAILURE'},
            _EXIT_REASONS_IGNORE={'OTHER'}, _EXIT_REASONS_FAIL_ONCE={'ANR','INITIALIZATION_FAILURE'},
            _EXIT_REASONS_CRASH_CLASS={'CRASH'})
        return namespace['_microg_exit_history'](b, {'com.android.vending', 'com.google.android.gms'}, 100)

    def record(self, timestamp, reason=6):
        return {'package':'com.android.vending', 'reasonCode':reason, 'timestamp':str(timestamp)}

    def test_prior_android_boot_anr_does_not_poison_current_soft_reboot(self):
        # Kernel boot 1999990000; system_server boot 1999999000.
        self.assertTrue(self.fixture([self.record(1999995000)])['ok'])

    def test_current_android_boot_anr_still_rejects(self):
        self.assertFalse(self.fixture([self.record(1999999500)])['ok'])

    def test_current_initialization_failure_still_rejects(self):
        self.assertFalse(self.fixture([self.record(1999999500, 7)])['ok'])

    def test_boundary_second_is_conservatively_retained(self):
        self.assertFalse(self.fixture([self.record(1999998999)])['ok'])
        self.assertTrue(self.fixture([self.record(1999998998)])['ok'])

    def test_system_server_restart_or_pid_reuse_during_observation_fails_closed(self):
        for ids in (((10,900000),(11,900000)), ((10,900000),(10,900001))):
            with self.subTest(ids=ids): self.assertFalse(self.fixture(identities=ids)['ok'])

    def test_missing_system_server_fails_closed(self):
        self.assertFalse(self.fixture(identities=(None,None))['ok'])

    def test_missing_or_invalid_guest_clock_frequency_fails_closed(self):
        for value in ('', '0', '-100', 'unknown'):
            with self.subTest(value=value):
                self.assertFalse(self.fixture(outputs={('getconf','CLK_TCK'): value})['ok'])

    def test_invalid_uptime_fails_closed(self):
        for value in ('nan', 'inf', '-1', '0', 'bad'):
            with self.subTest(value=value):
                self.assertFalse(self.fixture(outputs={('cat','/proc/uptime'): value})['ok'])

    def test_nonpositive_start_time_fails_closed(self):
        for value in (0,-1,'900000'):
            with self.subTest(value=value):
                self.assertFalse(self.fixture(identities=((10,value),(10,value)))['ok'])

    def test_future_exit_time_fails_closed(self):
        self.assertFalse(self.fixture([self.record(2000000002,1)])['ok'])

    def test_crash_loop_gate_uses_consistent_guest_clock(self):
        self.assertFalse(self.fixture([self.record(1999999900,4),self.record(1999999901,4)])['ok'])
        self.assertTrue(self.fixture([self.record(1999999100,4),self.record(1999999101,4)])['ok'])

    def test_ignored_normal_exit_is_retained_without_failure(self):
        self.assertTrue(self.fixture([self.record(1999999500,1)])['ok'])


if __name__ == '__main__':
    unittest.main()
