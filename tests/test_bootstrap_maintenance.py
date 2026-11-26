"""Actual candidate timer/Broker methods; native external services are doubles."""
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa'))
import qa_resource_runtime as q

# Reuse only existing public fixtures; run the actual production timer and
# Broker method. No private packet paths or retained live identities.
spec = importlib.util.spec_from_file_location('_bootstrap_public_proxy_fixtures', ROOT / 'tests/test_proxy_watchdog.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
host = base.host
EPOCH = base.EPOCH
COMPONENTS = ('root', 'keybox', 'proxy', 'location', 'camera')

class Manager(base.Manager):
    def __init__(self, state):
        super().__init__(state)
        self.bootstrap = {'ok': True, 'schema': 'dev.xenoid.daemon-bootstrap/v1', 'state': 'ready',
                          'generation': 1, 'instanceId': self.context.instance_id, 'runtimeEpoch': EPOCH,
                          'components': {name: {'ok': True, 'state': 'ready'} for name in COMPONENTS}}
        self.bootstrap_reads = 0
        self.bootstrap_get_error = False
        self.recover = True
        self.after_recover = None
        self.returned = None
        self.import_keys = Mock(side_effect=AssertionError('key import forbidden'))

    def daemon_client(self, **kwargs):
        return types.SimpleNamespace(proxy_status=self.proxy_status, bootstrap_status=self.bootstrap_status)

    def bootstrap_status(self, **kwargs):
        self.bootstrap_reads += 1
        if self.bootstrap_get_error:
            raise RuntimeError('private native error must not be copied')
        return copy.deepcopy(self.bootstrap)

    def reconcile_bootstrap(self, **kwargs):
        result = super().reconcile_bootstrap(**kwargs)
        if self.bootstrap_fails:
            return result
        self.bootstrap['generation'] += 1
        if self.recover:
            self.bootstrap.update(ok=True, state='ready')
            for name in COMPONENTS:
                self.bootstrap['components'][name] = {'ok': True, 'state': 'ready'}
        if self.after_recover:
            self.after_recover()
        result.update(ready=self.bootstrap['state'] == 'ready', state=self.bootstrap['state'],
                      generation=self.bootstrap['generation'], runtimeEpoch=self.bootstrap['runtimeEpoch'],
                      instanceId=self.bootstrap['instanceId'])
        if self.returned:
            result.update(self.returned)
        return result


class BootstrapMaintenance(unittest.TestCase):
    timer = base.Watchdog.timer

    def setUp(self):
        base.Watchdog.setUp(self)
        self.manager = Manager(self.root)
        self.broker = base.Broker(self.manager)

    def fail_component(self, name='keybox', state='failed'):
        self.manager.bootstrap.update(ok=False, state='degraded')
        self.manager.bootstrap['components'][name] = {
            'ok': False, 'state': state, 'errorCode': 'clear_failed', 'configured': False, 'active': False}

    def bootstrap_calls(self):
        return [value for value in self.manager.events if isinstance(value, tuple) and value[0] == 'official_bootstrap']

    def test_ready_bootstrap_is_observed_without_new_reconcile(self):
        value = self.timer()
        self.assertFalse(value['bootstrap_pending'])
        self.assertTrue(value['bootstrap_watchdog']['ready'])
        self.assertEqual(self.manager.bootstrap_reads, 1)
        self.assertEqual(self.bootstrap_calls(), [])

    def test_healthy_proxy_cannot_hide_unconfigured_keybox_failure(self):
        self.fail_component()
        value = self.timer()
        self.assertTrue(value['proxy_pending'])
        self.assertEqual(value['proxy_watchdog']['code'], 'proxy_post_bootstrap_unverified')
        self.assertTrue(value['resource_ok'])
        self.assertTrue(value['bootstrap_watchdog']['recovered'])
        self.assertFalse(value['bootstrap_pending'])
        self.assertEqual(len(self.bootstrap_calls()), 1)
        self.assertGreater(self.bootstrap_calls()[0][1], 170)
        self.assertEqual(self.manager.bootstrap_reads, 2)
        self.manager.import_keys.assert_not_called()
        self.fresh.ensure_runtime.assert_not_called()
        self.assertEqual(value['display'], self.previous['display'])
        fresh = self.timer()
        self.assertFalse(fresh['proxy_pending'])
        self.assertTrue(fresh['proxy_watchdog']['live_host_checked'])
        self.assertEqual(len(self.bootstrap_calls()), 1)

    def test_retry_is_generic_for_each_failed_native_component(self):
        for name in COMPONENTS:
            with self.subTest(component=name):
                self.manager.events.clear()
                self.fail_component(name)
                value = self.timer()
                self.assertTrue(value['bootstrap_watchdog']['recovered'])
                self.assertEqual(len(self.bootstrap_calls()), 1)

    def test_control_ready_does_not_misreport_aggregate_ready(self):
        self.fail_component()
        self.manager.recover = False
        value = self.timer()
        self.assertTrue(value['proxy_pending'])
        self.assertTrue(value['bootstrap_pending'])
        self.assertFalse(value['bootstrap_watchdog']['recovered'])
        self.assertEqual(value['bootstrap_watchdog']['failed_components'], ['keybox'])
        self.assertEqual(len(self.bootstrap_calls()), 1)

    def test_fresh_result_generation_mismatch_is_pending(self):
        self.fail_component()
        self.manager.returned = {'generation': 900}
        value = self.timer()
        self.assertTrue(value['bootstrap_pending'])
        self.assertEqual(value['bootstrap_watchdog']['code'], 'bootstrap_result_unconfirmed')

    def test_bool_generation_is_not_accepted_as_matching_integer(self):
        self.fail_component()
        self.manager.returned = {'generation': True}
        self.assertTrue(self.timer()['bootstrap_pending'])

    def test_fresh_result_instance_mismatch_is_pending(self):
        self.fail_component()
        self.manager.returned = {'instanceId': 'different'}
        self.assertTrue(self.timer()['bootstrap_pending'])

    def test_post_recovery_get_failure_is_pending_without_second_mutation(self):
        self.fail_component()
        self.manager.after_recover = lambda: setattr(self.manager, 'bootstrap_get_error', True)
        value = self.timer()
        self.assertTrue(value['bootstrap_pending'])
        self.assertTrue(value['proxy_pending'])
        self.assertEqual(len(self.bootstrap_calls()), 1)
        self.assertNotIn('private native error', str(value))

    def test_initial_unreadable_or_invalid_status_never_authorizes_bootstrap(self):
        for status in (None, {}, {'ok': False, 'code': 'daemon_unreachable'}):
            self.manager.bootstrap = status
            value = self.timer()
            self.assertTrue(value['bootstrap_pending'])
            self.assertFalse(value['proxy_pending'])
            self.assertEqual(self.bootstrap_calls(), [])

    def test_inflight_bootstrap_is_not_restarted(self):
        for state in ('accepted', 'reconciling'):
            self.fail_component()
            self.manager.bootstrap['state'] = state
            self.assertTrue(self.timer()['bootstrap_pending'])
            self.assertEqual(self.bootstrap_calls(), [])

    def test_healthy_unconfigured_component_is_not_failure(self):
        self.manager.bootstrap['components']['keybox'].update(state='unconfigured', configured=False, active=False)
        self.assertFalse(self.timer()['bootstrap_pending'])
        self.assertEqual(self.bootstrap_calls(), [])

    def test_epoch_mismatch_cannot_authorize_retry(self):
        self.fail_component()
        self.manager.bootstrap['runtimeEpoch'] = 'c' * 64
        value = self.timer()
        self.assertTrue(value['bootstrap_pending'])
        self.assertEqual(value['bootstrap_watchdog']['code'], 'bootstrap_epoch_unconfirmed')
        self.assertEqual(self.bootstrap_calls(), [])

    def test_real_userspace_epoch_change_is_recorded_not_disguised(self):
        self.fail_component()
        def changed():
            self.manager.bootstrap.update(runtimeEpoch='c' * 64)
            self.manager.status.update(runtimeEpoch='c' * 64, quarantined=True)
        self.manager.after_recover = changed
        value = self.timer()
        self.assertTrue(value['bootstrap_watchdog']['runtime_epoch_changed'])
        self.assertTrue(value['bootstrap_watchdog']['recovered'])
        self.assertTrue(self.manager.status['quarantined'])
        self.assertTrue(value['proxy_pending'])
        self.assertFalse(value['proxy_watchdog']['ready'])
        # Candidate never manipulates a reader/offer; existing actual changed-
        # epoch UI guard remains independently mandatory for the next input.
        self.assertEqual(len(self.bootstrap_calls()), 1)

    def test_ready_bootstrap_different_epoch_cannot_prove_old_proxy_ready(self):
        self.manager.bootstrap['runtimeEpoch'] = 'c' * 64
        value = self.timer()
        self.assertFalse(value['bootstrap_pending'])
        self.assertTrue(value['proxy_pending'])
        self.assertEqual(value['proxy_watchdog']['code'], 'proxy_bootstrap_epoch_unconfirmed')
        self.assertEqual(self.bootstrap_calls(), [])

    def test_changed_CID_during_recovery_remains_fatal(self):
        self.fail_component()
        self.manager.after_recover = lambda: setattr(self.manager, 'owned', {**self.manager.owned, 'Id': 'd' * 64})
        with self.assertRaises(host.RuntimeIdentityError):
            self.timer()

    def test_missing_proxy_epoch_path_does_not_double_bootstrap_same_tick(self):
        self.fail_component()
        self.manager.status['runtimeEpoch'] = ''
        self.manager.recover = False
        value = self.timer()
        self.assertTrue(value['bootstrap_pending'])
        self.assertEqual(len(self.bootstrap_calls()), 1)

    def test_persistent_failure_gets_one_retry_per_existing_tick_without_spin(self):
        self.fail_component()
        self.manager.recover = False
        first = self.timer()
        self.assertEqual(len(self.bootstrap_calls()), 1)
        second = self.timer()
        self.assertTrue(first['bootstrap_pending'])
        self.assertTrue(second['bootstrap_pending'])
        self.assertEqual(len(self.bootstrap_calls()), 2)


if __name__ == '__main__':
    unittest.main()
