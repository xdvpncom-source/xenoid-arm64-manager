"""Final teardown waits only for native lock entry, never replays cleanup/input."""
import contextlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import script_host as h


class InstanceError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Clock:
    now = 0
    def monotonic(self): return self.now


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.entered = 0
        self.failures = 3
        self.error = lambda: InstanceError('instance_busy', 'instance operation in progress')
        self.reader = Mock()
        self.log = Mock()
        self.broker = h.Broker(types.SimpleNamespace(), self.log, config={'command_timeout': 1})
        self.broker._ui_reader = self.reader
        self.clock_patch = patch.object(h, 'time', self.clock)
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        self.config_patch = patch.dict(sys.modules, {'xenoid.config': types.SimpleNamespace(InstanceError=InstanceError)})
        self.config_patch.start(); self.addCleanup(self.config_patch.stop)

        @contextlib.contextmanager
        def locked(*, timeout_seconds):
            self.entered += 1
            self.assertIs(self.broker._ui_reader, self.reader)
            if self.entered <= self.failures:
                self.assertFalse(self.reader.close.called)
                self.clock.now += timeout_seconds
                raise self.error()
            yield

        self.reader.manager.instance_operation_lock.side_effect = locked

    def test_busy_then_free_retains_reader_and_closes_once(self):
        self.broker.close_ui_reader()
        self.assertEqual(self.entered, 4)
        self.reader.close.assert_called_once_with(force=False)
        self.assertIsNone(self.broker._ui_reader)
        self.log.assert_called_once()

    def test_final_cleanup_drain_waits_past_ordinary_budget_without_next_action(self):
        self.failures = 55  # 13.75s, longer than the old hardcoded 12s wait.
        self.broker.stopping.set()
        self.broker.cleanup(drain=True)
        self.assertEqual(self.entered, 56)
        self.assertGreater(self.clock.now, 12)
        self.reader.close.assert_called_once_with(force=False)
        self.assertIsNone(self.broker._ui_reader)
        self.assertLessEqual(self.log.call_count, 3)

    def test_ordinary_cleanup_honors_finite_budget_and_retains_reader(self):
        self.failures = 99
        with self.assertRaisesRegex(h.TransportUnavailable, 'configured timeout'):
            self.broker.close_ui_reader()
        self.assertEqual(self.clock.now, 1)
        self.reader.close.assert_not_called()
        self.assertIs(self.broker._ui_reader, self.reader)

    def test_cleanup_body_failure_is_not_retried_or_detached(self):
        self.failures = 0
        self.reader.close.side_effect = InstanceError('instance_busy', 'instance operation in progress')
        with self.assertRaises(InstanceError): self.broker.close_ui_reader(drain=True)
        self.assertEqual(self.entered, 1)
        self.reader.close.assert_called_once()
        self.assertIs(self.broker._ui_reader, self.reader)

    def test_other_native_lock_errors_are_not_reclassified(self):
        self.error = lambda: InstanceError('instance_busy', 'instance operation lock invalid')
        with self.assertRaisesRegex(InstanceError, 'lock invalid'):
            self.broker.close_ui_reader(drain=True)
        self.assertEqual(self.entered, 1)
        self.reader.close.assert_not_called()
        self.assertIs(self.broker._ui_reader, self.reader)

    def test_same_text_from_non_native_exception_is_not_retried(self):
        class Lookalike(RuntimeError):
            code = 'instance_busy'
        self.error = lambda: Lookalike('instance operation in progress')
        with self.assertRaises(Lookalike): self.broker.close_ui_reader(drain=True)
        self.assertEqual(self.entered, 1)
        self.reader.close.assert_not_called()

    def test_zero_ordinary_budget_keeps_waiting_without_a_hidden_ceiling(self):
        self.broker.config['command_timeout'] = 0
        self.failures = 8
        self.broker.close_ui_reader()
        self.assertEqual(self.entered, 9)
        self.reader.close.assert_called_once_with(force=False)

    def test_explicit_shutdown_propagates_without_cleanup_or_detach(self):
        self.error = h.Shutdown
        with self.assertRaises(h.Shutdown): self.broker.close_ui_reader(drain=True)
        self.assertEqual(self.entered, 1)
        self.reader.close.assert_not_called()
        self.assertIs(self.broker._ui_reader, self.reader)


if __name__ == '__main__': unittest.main()
