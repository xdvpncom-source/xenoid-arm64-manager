"""Fresh readback through an explicit current-worker/old-receipt recovery scope."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_waiting_native_identity as waiting


class RecoveryIdentityObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = waiting.WaitingNativeIdentity()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        self.current = 'c' * 32
        self.current_config = {**f.config, 'run_id': self.current, 'source': 'new scenario'}
        with closing(sqlite3.connect(str(f.database))) as db, db:
            db.execute('UPDATE runs SET current=0,state=?', ('stopped',))
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                       (self.current, json.dumps(self.current_config), 'waiting', 0, 1))
        self.envelope = {'schema': 'dev.xenoid.native-identity-recovery/v1',
            'current_run': self.current, 'current_iteration': 0,
            'current_config_sha256': hashlib.sha256(json.dumps(self.current_config).encode()).hexdigest(),
            'original_run': f.run, 'original_iteration': 1,
            'original_config_sha256': hashlib.sha256(json.dumps(f.config).encode()).hexdigest(),
            'operation_key': f.key, 'receipt_sha256': hashlib.sha256(json.dumps(f.receipt).encode()).hexdigest(),
            'binding': f.target, 'worker': {'pid': f.worker, 'start_ticks': 567,
                                          'unit': 'xenoid-panel-script@1.service'},
            'request_sha256': f.request['request_sha256'], 'proof_sha256': '1' * 64}
        self.recovered = {'request': f.request, 'receipt': f.receipt,
                          'original_config': f.config, 'current_config': self.current_config,
                          'binding': f.target, 'worker': self.envelope['worker']}
        self.calls = []
        def envelope(folder, current, *, remaining):
            self.calls.append(('envelope', current, remaining(2)))
            return copy.deepcopy(self.envelope)
        def validate(folder, current, value, *, remaining):
            self.calls.append(('validate', current, remaining(2)))
            self.assertEqual(value, self.envelope)
            return copy.deepcopy(self.recovered)
        self.helper_patches = (
            patch.object(waiting.identity, 'recovery_envelope', side_effect=envelope, create=True),
            patch.object(waiting.identity, 'validate_recovery_envelope', side_effect=validate, create=True))
        for item in self.helper_patches:
            item.start(); self.addCleanup(item.stop)

    def observe(self):
        return self.fixture.observe()

    def reject(self):
        result = self.observe()
        self.assertNotEqual(result['status'], 'observed', result)
        self.assertNotIn('observation', result)
        return result

    def test_current_worker_can_publish_actual_fresh_old_before_values(self):
        f = self.fixture
        before = {p: p.read_bytes() for p in (f.database, f.request_path, f.root_run / 'active.json')}
        with patch.object(waiting.host.time, 'time', return_value=2345678901.75):
            result = self.observe()
        self.assertEqual(result['status'], 'observed', result)
        proof = result['observation']
        self.assertEqual(proof['values'], waiting.idle.VALUES)
        self.assertEqual(proof['captured_at'], 2345678901.75)
        self.assertEqual(proof['request_id'], 'b' * 32)
        self.assertTrue(f.fixture.timeouts, 'must perform a real new read')
        self.assertTrue(all(0 < call[2] <= 8 for call in self.calls))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(set(proof), {'schema', 'binding', 'request_id', 'runtime', 'values',
                                     'build', 'captured_at', 'proof_sha256'})

    def test_real_recovery_helper_and_guard_share_old_request_without_relabeling(self):
        for item in self.helper_patches: item.stop()
        f = self.fixture
        profile = json.loads((waiting.HERE / 'fixtures/identity-native-cellular.json').read_text())['profiles'][0]['profile']
        target = {'simEpoch': 'e' * 32, 'locationProfileDigest': profile['identityDigest']}
        request = {**f.request, 'target': target, 'target_sha256': waiting.identity.digest(target),
            'location_sidecar': {'simEpoch': 'e' * 32, 'country': 'SG', 'profile': profile,
                                 'profileDigest': profile['identityDigest']}}
        request = waiting.identity.signed({k: v for k, v in request.items() if k != 'request_sha256'}, 'request_sha256')
        f.write(f.request_path, request)
        f.write(f.root_run / 'active.json', {'path': str(f.request_path.relative_to(f.root_run)),
                                           'sha256': request['request_sha256']})
        original = f.request_path.read_bytes()
        with patch.object(waiting.host.time, 'time', return_value=2345678902.5):
            result = self.observe()
        self.assertEqual(result['status'], 'observed', result)
        self.assertEqual(result['observation']['captured_at'], 2345678902.5)
        self.assertEqual(result['observation']['values'], waiting.idle.VALUES)
        self.assertEqual(f.request_path.read_bytes(), original)
        self.assertTrue(f.fixture.timeouts)

    def test_envelope_worker_must_match_real_pid_start_and_unit(self):
        for key, value in (('pid', 9999), ('start_ticks', 568), ('unit', 'other.service')):
            with self.subTest(key=key):
                old = self.envelope['worker'][key]
                self.envelope['worker'][key] = value
                self.reject()
                self.envelope['worker'][key] = old

    def test_changed_current_config_rejects_before_android_read(self):
        f = self.fixture
        with closing(sqlite3.connect(str(f.database))) as db, db:
            db.execute('UPDATE runs SET config=? WHERE current=1',
                       (json.dumps({**self.current_config, 'source': 'replacement'}),))
        self.reject(); self.assertFalse(f.fixture.timeouts)

    def test_queued_current_worker_can_recover_before_first_app_iteration(self):
        f = self.fixture
        with closing(sqlite3.connect(str(f.database))) as db, db:
            db.execute('UPDATE runs SET state=? WHERE current=1', ('queued',))
        result = self.observe()
        self.assertEqual(result['status'], 'observed', result)
        self.assertTrue(f.fixture.timeouts)

    def test_queued_state_does_not_broaden_normal_same_run_reserve(self):
        f = self.fixture
        with closing(sqlite3.connect(str(f.database))) as db, db:
            db.execute('DELETE FROM runs WHERE id=?', (self.current,))
            db.execute('UPDATE runs SET state=?,current=1 WHERE id=?', ('queued', f.run))
        self.reject()
        self.assertFalse(f.fixture.timeouts)

    def test_changed_old_receipt_rejects_before_android_read(self):
        self.fixture.update_receipt(before_digest='0' * 64)
        self.reject(); self.assertFalse(self.fixture.fixture.timeouts)

    def test_13_values_are_not_substituted_from_old_request(self):
        original = waiting.identity.observe
        def changed(reader):
            values, build = original(reader)
            return {**values, 'serial': 'FRESH-DRIFT'}, build
        with patch.object(waiting.identity, 'observe', side_effect=changed): self.reject()

    def test_recovery_envelope_change_after_actual_read_discards_result(self):
        original = waiting.identity.observe
        def changed(reader):
            result = original(reader)
            self.envelope['proof_sha256'] = '2' * 64
            return result
        with patch.object(waiting.identity, 'observe', side_effect=changed): self.reject()

    def test_finalization_compares_fresh_target_after_committed_native_journal(self):
        f = self.fixture
        expected = {**waiting.idle.VALUES, 'serial': 'NEW-SERIAL'}
        target = {'stable': {'serial': 'NEW-SERIAL'}}
        state = {'phase': 'committed', 'transactionId': f.request['native_transaction'],
                 'before': f.request['native_before'], 'target': target}
        request = {**f.request, 'phase': 'finalize', 'target': target, 'target_sha256': '9' * 64,
                   'location_sidecar': {}, 'native_state': state, 'values': expected}
        request = waiting.identity.signed({k: v for k, v in request.items() if k != 'request_sha256'}, 'request_sha256')
        path = f.operation / 'attempt-0.finalize.json'
        f.write(path, request)
        f.write(f.root_run / 'active.json', {'path': str(path.relative_to(f.root_run)), 'sha256': request['request_sha256']})
        f.write(f.operation / 'prepared.json', {'native_transaction': request['native_transaction'],
                'target_sha256': request['target_sha256'], 'attempt': request['attempt']})
        f.update_receipt(native_journal=state)
        self.recovered.update(request=request, receipt=f.receipt)
        self.envelope['request_sha256'] = request['request_sha256']
        original = waiting.identity.observe
        def fresh(reader):
            _, build = original(reader)
            return dict(expected), build
        with patch.object(waiting.identity, 'values_from_target', return_value=expected), \
                patch.object(waiting.identity, 'observe', side_effect=fresh):
            result = self.observe()
        self.assertEqual(result['status'], 'observed', result)
        self.assertEqual(result['observation']['values'], expected)
        with patch.object(waiting.identity, 'values_from_target', return_value=expected):
            self.reject()  # The old live values may not stand in for committed target values.


if __name__ == '__main__': unittest.main()
