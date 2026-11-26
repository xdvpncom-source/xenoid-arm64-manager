"""A new scenario revision can finish an older recorded GEN without running it."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import script_host as h
from identity_reservations import digest


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        h.prepare(self.folder)
        self.old = {'run_id': 'old-run', 'instance_id': '11111111-1111-1111-1111-111111111111',
                    'resource_tag': '0123456789ab', 'expected_container_id': 'a' * 64,
                    'source': "raise AssertionError('old user source must not run')", 'identity_reservations': True,
                    'interval': 0}
        self.current = {**self.old, 'run_id': 'new-run', 'source': 'print("new scenario")',
                        'identity_reservations': False}
        self.binding = {'instance_id': self.old['instance_id'], 'resource_tag': self.old['resource_tag'],
                        'container_id': self.old['expected_container_id']}
        self.receipt = {'status': 'pending', 'binding': self.binding, 'timeout': 0,
                        'identity_reservation_context': {
                            'scenario_sha256': hashlib.sha256(self.old['source'].encode()).hexdigest(),
                            'profile_plan_sha256': None}}
        with h.db(self.folder) as conn:
            for cfg, active in ((self.old, 0), (self.current, 1)):
                conn.execute('INSERT INTO runs(id,created,config,state,current,updated) VALUES(?,?,?,?,?,?)',
                             (cfg['run_id'], h.now(), json.dumps(cfg), 'queued', active, h.now()))
        h.RegenerationReceipts(self.folder, 'old-run', 3).save('same-key', self.receipt)
        self.manager = types.SimpleNamespace(context=types.SimpleNamespace(
            instance_id=self.old['instance_id'], resource_tag=self.old['resource_tag']))

    def save_old(self, value):
        with h.db(self.folder) as conn:
            conn.execute('UPDATE runs SET config=? WHERE id=?', (json.dumps(value), 'old-run'))

    def replay(self, fail_once=False):
        calls = []
        outer = self

        class Broker:
            def __init__(self, manager, log, verify, *, config, app_cache):
                self.config = config
                self.verify = verify

            def regenerate(self, timeout, key, cache, **options):
                calls.append((copy.deepcopy(self.config), key))
                self.verify()
                if fail_once and len(calls) == 1:
                    with h.db(outer.folder) as conn:
                        conn.execute('UPDATE runs SET state=? WHERE current=1', ('stopped',))
                    raise h.RegenerationPending('native_phase_pending')

        with patch.object(h, 'trusted_device', return_value=self.manager), \
             patch.object(h, 'Broker', Broker), \
             patch.object(h, 'jail_layout', side_effect=AssertionError('No old or new script execution')):
            result = h.resume_regeneration_receipts(self.folder, 1, self.current, 0, {})
        return result, calls

    def test_new_revision_replays_original_source_and_reservation_flag(self):
        result, calls = self.replay()
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        pinned, key = calls[0]
        self.assertEqual(key, 'same-key')
        self.assertEqual(pinned['run_id'], 'old-run')
        self.assertEqual(pinned['source'], self.old['source'])
        self.assertTrue(pinned['identity_reservations'])
        self.assertEqual(h.RegenerationReceipts(self.folder, 'old-run', 3).load(key), self.receipt)
        with h.db(self.folder) as conn:
            stored = {row['id']: json.loads(row['config']) for row in conn.execute('SELECT id,config FROM runs')}
        self.assertEqual(stored, {'old-run': self.old, 'new-run': self.current})

    def test_new_reservation_setting_is_not_enabled_for_legacy_receipt(self):
        self.old.pop('identity_reservations')
        self.save_old(self.old)
        self.receipt.pop('identity_reservation_context')
        h.RegenerationReceipts(self.folder, 'old-run', 3).save('same-key', self.receipt)
        self.current['identity_reservations'] = True
        with h.db(self.folder) as conn:
            conn.execute('UPDATE runs SET config=? WHERE current=1', (json.dumps(self.current),))
        _, calls = self.replay()
        self.assertFalse(calls[0][0]['identity_reservations'])
        self.assertEqual(calls[0][0]['source'], self.old['source'])

    def test_original_profile_context_is_verified(self):
        self.receipt['profile_plan'] = {'settings': {'same': True}}
        self.receipt['identity_reservation_context']['profile_plan_sha256'] = digest(self.receipt['profile_plan'])
        actual = h.regeneration_replay_config(self.folder, 'old-run', self.binding, self.receipt)
        self.assertEqual(actual['source'], self.old['source'])
        self.receipt['profile_plan']['settings']['same'] = False
        with self.assertRaisesRegex(h.RegenerationPending, 'original_config_invalid'):
            h.regeneration_replay_config(self.folder, 'old-run', self.binding, self.receipt)

    def test_missing_original_run_never_falls_back_to_current_source(self):
        with h.db(self.folder) as conn:
            conn.execute('DELETE FROM runs WHERE id=?', ('old-run',))
        with self.assertRaisesRegex(h.RegenerationPending, 'original_run_missing'):
            self.replay()

    def test_invalid_original_metadata_never_reaches_broker(self):
        changes = ({'run_id': 'other'}, {'instance_id': 'other'}, {'resource_tag': 'other'},
                   {'expected_container_id': 'b' * 64}, {'source': None}, {'source': ''},
                   {'source': '\ud800'}, {'identity_reservations': 'true'}, {'identity_reservations': False})
        for change in changes:
            with self.subTest(change=list(change)):
                self.save_old({**self.old, **change})
                with self.assertRaisesRegex(h.RegenerationPending, 'original_config_invalid'):
                    self.replay()
        self.save_old(['not', 'an', 'object'])
        with self.assertRaisesRegex(h.RegenerationPending, 'original_config_invalid'):
            self.replay()
        with h.db(self.folder) as conn:
            conn.execute('UPDATE runs SET config=? WHERE id=?', ('not json', 'old-run'))
        with self.assertRaisesRegex(h.RegenerationPending, 'original_config_invalid'):
            self.replay()

    def test_different_recorded_scenario_hash_is_rejected(self):
        self.receipt['identity_reservation_context']['scenario_sha256'] = 'b' * 64
        h.RegenerationReceipts(self.folder, 'old-run', 3).save('same-key', self.receipt)
        with self.assertRaisesRegex(h.RegenerationPending, 'original_config_invalid'):
            self.replay()

    def test_current_run_stop_still_interrupts_old_receipt_retry(self):
        with self.assertRaises(h.Shutdown):
            self.replay(fail_once=True)


if __name__ == '__main__':
    unittest.main()
