"""Control upgrade lifecycle; native mutation methods are recorded without running them."""
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('proxy_control_upgrade', ROOT / 'scripts/upgrade-proxy-control.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class Manager:
    def __init__(self):
        self.context = types.SimpleNamespace(instance_id='11111111-1111-4111-8111-111111111111', resource_tag='0123456789ab')
        self.cid, self.data, self.running = 'a'*64, 'same-data', True
        self.source = {'kind': 'endpoint', 'text': 'private-credential-test-value', 'selectedNode': 'node-a'}
        self.status = {'ok': True, 'stateReadable': True, 'configured': True, 'enabled': True,
                       'udpAllowed': False, 'ipv6Allowed': True, 'generation': 1}
        self._proxy_installed_helper = '/usr/libexec/xenoid-proxy-engine.py'
        self.calls, self.lock_requests, self.locked = [], [], False
        self.engine_sha, self.control, self.installed = 'old', 'desired-control', 'old'
        self.pending, self.proof_ok, self.failure = False, True, None
        self.process = {'pid': 100, 'starttime': 1, 'pythonPath': '/usr/bin/python3', 'pythonSha256': 'f'*64}
        self.mutate = lambda: None
    @contextmanager
    def instance_operation_lock(self, timeout_seconds=None):
        self.lock_requests.append(timeout_seconds)
        self.locked = True
        try: yield
        finally: self.locked = False
    def _owned_container_record(self, timeout):
        return {'Id': self.cid, 'State': {'Running': self.running},
                'Mounts': [{'Destination': '/data', 'Name': self.data}]}, None
    def daemon_client(self): return self
    def proxy_status(self): return copy.deepcopy(self.status)
    def proxy_export(self): return {'ok': True, 'source': copy.deepcopy(self.source)}
    def _proxy_local_control_digest(self): return self.control
    def _proxy_root_json(self, action):
        assert self.locked and action == 'check-control'
        return {'ok': True, 'controlDigest': self.installed}
    def _proxy_remote_sha256(self, path): return self.engine_sha
    def proxy_prerequisite(self):
        assert self.locked
        self.calls.append('prerequisite')
        self.installed, self.engine_sha = self.control, self.target_sha
        return {'ok': True}
    def proxy_quarantine(self):
        assert self.locked
        self.calls.append('quarantine')
        return {'ok': True}
    def _proxy_stop_agent(self):
        assert self.locked
        self.calls.append('stop')
        return True
    def reconcile_proxy_desired(self):
        assert self.locked
        self.calls.append('reconcile')
        if self.failure: return {'ok': False, 'error': self.failure}
        self.process = {**self.process, 'pid': self.process['pid']+1, 'starttime': self.process['starttime']+1}
        self.mutate()
        return {'ok': True}


class ProxyControlUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.engine = root / 'upstream/scripts/xenoid-proxy-engine.py'
        self.engine.parent.mkdir(parents=True)
        self.engine.write_text('verified-current-engine')
        self.state = root / 'state'
        self.enterContext(patch.object(upgrade, 'UPSTREAM', root / 'upstream'))
        self.enterContext(patch.object(upgrade, 'STATE', self.state))
        self.enterContext(patch.object(upgrade, 'process', lambda manager: copy.deepcopy(manager.process)))
        self.manager = Manager()
        self.manager.target_sha = hashlib.sha256(self.engine.read_bytes()).hexdigest()
        self.broker = types.SimpleNamespace(display_raw_docker=lambda *args: nullcontext())
    def proof(self, manager, client, deadline):
        assert manager.locked
        upgrade.require(manager.proof_ok, 'proxy_data_plane_unverified')
    def run_upgrade(self, timeout=180, manager=None):
        return upgrade.refresh(manager or self.manager, self.broker, timeout=timeout,
            pending=lambda current: current.pending, proof=self.proof)
    def marker(self): return self.state / (self.manager.context.instance_id + '.json')
    def test_first_upgrade_rebootstraps_once_and_same_current_process_is_idempotent(self):
        before = copy.deepcopy(self.manager.source)
        self.assertTrue(self.run_upgrade()['changed'])
        self.assertEqual(self.manager.calls, ['prerequisite', 'quarantine', 'stop', 'reconcile'])
        self.assertEqual(self.manager.source, before)
        self.assertEqual(self.run_upgrade()['skipped'], 'already_current')
        self.assertEqual(len(self.manager.calls), 4)
        saved = json.loads(self.marker().read_text())
        self.assertEqual(saved['process'], self.manager.process)
        self.assertNotIn('private-credential', self.marker().read_text())
    def test_later_instance_reloads_even_when_global_control_disk_already_matches(self):
        self.run_upgrade()
        another = Manager()
        another.context = types.SimpleNamespace(instance_id='22222222-2222-4222-8222-222222222222', resource_tag='abcdef012345')
        another.target_sha = self.manager.target_sha
        another.installed, another.engine_sha = another.control, another.target_sha
        self.assertTrue(self.run_upgrade(manager=another)['changed'])
        self.assertEqual(another.calls, ['prerequisite', 'quarantine', 'stop', 'reconcile'])
    def test_process_replacement_or_engine_source_revision_reloads_once(self):
        self.run_upgrade()
        self.manager.process['pid'] += 20
        self.assertTrue(self.run_upgrade()['changed'])
        self.engine.write_text('new-source-revision')
        self.manager.target_sha = hashlib.sha256(self.engine.read_bytes()).hexdigest()
        self.assertTrue(self.run_upgrade()['changed'])
        self.assertEqual(self.manager.calls.count('stop'), 3)
    def test_stopped_unconfigured_and_disabled_instances_are_not_started_or_mutated(self):
        for state in ('stopped', 'unconfigured', 'disabled'):
            with self.subTest(state=state):
                manager = Manager()
                manager.target_sha = self.manager.target_sha
                if state == 'stopped': manager.running = False
                else: manager.status['configured' if state == 'unconfigured' else 'enabled'] = False
                self.assertIn('skipped', self.run_upgrade(manager=manager))
                self.assertEqual(manager.calls, [])
    def test_pending_native_identity_transaction_is_never_stopped(self):
        self.manager.pending = True
        with self.assertRaisesRegex(upgrade.UpgradeError, 'native_regeneration_pending'): self.run_upgrade()
        self.assertEqual(self.manager.calls, [])
    def test_configured_timeout_and_zero_use_the_same_native_lock(self):
        self.run_upgrade(timeout=0)
        self.run_upgrade(timeout=27)
        self.assertEqual(self.manager.lock_requests, [None, 27])
    def test_failed_reconcile_or_proof_keeps_no_marker_and_quarantines_original_runtime(self):
        for failure in ('reconcile', 'proof'):
            with self.subTest(failure=failure):
                manager = Manager(); manager.target_sha = self.manager.target_sha
                if failure == 'reconcile': manager.failure = 'proxy_unavailable'
                else: manager.proof_ok = False
                with self.assertRaises(upgrade.UpgradeError): self.run_upgrade(manager=manager)
                self.assertEqual(manager.calls[-1], 'quarantine')
                self.assertFalse(self.marker().exists())
    def test_source_or_data_change_fails_closed_and_never_marks_success(self):
        for changed in ('source', 'data'):
            with self.subTest(changed=changed):
                manager = Manager(); manager.target_sha = self.manager.target_sha
                if changed == 'source': manager.mutate = lambda: manager.source.update(selectedNode='unexpected')
                else: manager.mutate = lambda: setattr(manager, 'data', 'replacement-data')
                with self.assertRaisesRegex(upgrade.UpgradeError, 'proxy_upgrade_binding_changed'): self.run_upgrade(manager=manager)
                self.assertFalse(self.marker().exists())
    def test_lost_container_binding_never_quarantines_the_replacement(self):
        self.manager.mutate = lambda: setattr(self.manager, 'cid', 'b'*64)
        with self.assertRaisesRegex(upgrade.UpgradeError, 'proxy_upgrade_binding_changed'): self.run_upgrade()
        self.assertEqual(self.manager.calls[-1], 'reconcile')
        self.assertFalse(self.marker().exists())
    def test_matching_marker_still_requires_current_live_proof(self):
        self.run_upgrade()
        self.manager.proof_ok = False
        with self.assertRaisesRegex(upgrade.UpgradeError, 'proxy_data_plane_unverified'): self.run_upgrade()
        self.assertEqual(self.manager.calls.count('stop'), 1)
        self.assertEqual(self.manager.calls[-1], 'quarantine')
        self.assertFalse(self.marker().exists())
        self.manager.proof_ok = True
        self.assertTrue(self.run_upgrade()['changed'])
    def test_matching_marker_does_not_hide_installed_file_or_control_drift(self):
        self.run_upgrade()
        self.manager.installed = 'unexpected-control'
        self.assertTrue(self.run_upgrade()['changed'])
        self.manager.engine_sha = 'unexpected-helper'
        self.assertTrue(self.run_upgrade()['changed'])
    def test_corrupt_marker_rebootstraps_instead_of_claiming_current(self):
        self.state.mkdir()
        self.marker().write_text('partial-write')
        self.assertTrue(self.run_upgrade()['changed'])


if __name__ == '__main__': unittest.main()
