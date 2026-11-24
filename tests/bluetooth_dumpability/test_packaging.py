"""Always check public inputs; run real kernel cases only on a suitable host."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import run_kernel_tests as runner
except ImportError:
    import run_kernel_tests as runner


class SourceFixtureTests(unittest.TestCase):
    def test_public_defaults_pin_the_production_source_and_original_fixture(self):
        source, fixture = runner.checked_inputs()
        policy = runner.block(source.decode())
        self.assertIn('static void bluetooth_initialize_dumpability(void)', policy)
        self.assertIn('int prctl(int option, ...)', policy)
        self.assertIn('#include "prctl-policy-under-test.inc"', fixture.decode())
        self.assertEqual(len(runner.CASES), 10)

    def test_changed_input_is_rejected_before_any_child_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / 'changed.c'
            changed.write_bytes(runner.SOURCE.read_bytes() + b'\n')
            output = Path(temporary) / 'output'
            with mock.patch.object(runner.subprocess, 'run') as child:
                with self.assertRaisesRegex(RuntimeError, 'test_source_pin'):
                    runner.run_tests(output, source=changed)
                child.assert_not_called()
            self.assertFalse(output.exists())

    def test_existing_policy_is_read_only_and_other_values_are_unsupported(self):
        with mock.patch.object(runner.sys, 'platform', 'linux'), \
             mock.patch.object(runner.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(runner.os, 'geteuid', return_value=0), \
             mock.patch.object(runner.Path, 'read_text', return_value='0\n') as read:
            self.assertIn('already be 2', runner.unsupported_reason())
            read.assert_called_once_with()


class RealKernelTests(unittest.TestCase):
    def test_all_ten_production_policy_cases(self):
        reason = runner.unsupported_reason()
        if reason:
            self.skipTest(reason)
        with tempfile.TemporaryDirectory(prefix='xenoid-bt-policy-') as temporary:
            result = runner.run_tests(Path(temporary) / 'output')
            self.assertTrue(result['ok'], result['cases'])
            self.assertEqual(len(result['cases']), 10)
            self.assertEqual(result['sysctl_mutations'], 0)


if __name__ == '__main__':
    unittest.main()
