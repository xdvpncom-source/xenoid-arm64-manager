"""A busy predecessor identity proof cannot block fresh start admission."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa'))
spec = importlib.util.spec_from_file_location('start_identity_snapshot53', ROOT / 'qa/script_host.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
import identity_reservations as identities


class StartSnapshotIdentityMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / '1'
        h.prepare(self.folder)
        self.old, self.new = 'b' * 32, 'c' * 32
        self.config = {'script_id': 3, 'version': 1, 'language': 'python', 'source': "print('ready')",
                       'run_id': self.new, 'resource_tag': 'a' * 12,
                       'instance_id': '11111111-1111-1111-1111-111111111111',
                       'expected_container_id': 'd' * 64, 'repeat_count': 1,
                       'initial_delay': 0, 'interval': 0, 'timeout': 0, 'command_timeout': 0,
                       'verify_apk': False, 'identity_reservations': True}
        previous = h.validate({**self.config, 'run_id': self.old})
        with h.db(self.folder) as db:
            db.execute("INSERT INTO runs(id,created,config,state,iteration,current,updated,resume_at) VALUES(?,?,?,'stopped',7,1,?,0)",
                       (self.old, h.now(), json.dumps(previous), h.now()))
        self.status = patch('qa_profile_runtime.status_for_binding', return_value=None)
        self.status.start()
        self.addCleanup(self.status.stop)

    def busy_old(self, folder, run):
        if run == self.old:
            raise identities.ReservationPending('identity_terminal_native_busy')
        return {}

    def test_start_uses_old_state_without_requesting_busy_identity_metadata(self):
        with patch.object(identities, 'poll', side_effect=self.busy_old) as poll, \
             patch.object(h, 'install_unit'), patch.object(h, 'trusted_device') as trusted, \
             patch.object(h, 'ctl') as control, patch.object(h.sys, 'stdin', io.StringIO(json.dumps(self.config))):
            result = h.manage('start', self.folder, 1, None)
        self.assertEqual(result['run_id'], self.new)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual([call.args[1] for call in poll.call_args_list], [self.new])
        trusted.assert_called_once_with(1, h.validate(self.config))
        self.assertEqual([call.args for call in control.call_args_list],
                         [('stop', h.unit(1)), ('enable', '--now', h.unit(1))])
        with h.db(self.folder) as db:
            self.assertEqual(db.execute('SELECT id FROM runs WHERE current=1').fetchone()[0], self.new)
            self.assertEqual(db.execute('SELECT state FROM runs WHERE id=?', (self.old,)).fetchone()[0], 'stopped')

    def test_direct_snapshot_still_requires_the_full_identity_proof_by_default(self):
        with patch.object(identities, 'poll', side_effect=self.busy_old):
            with self.assertRaisesRegex(identities.ReservationPending, 'identity_terminal_native_busy'):
                h.snapshot(self.folder, 1, self.old)

    def test_public_poll_still_propagates_busy_identity_proof(self):
        with patch.object(h, 'STATE', self.root), patch.object(identities, 'poll', side_effect=self.busy_old):
            with self.assertRaisesRegex(identities.ReservationPending, 'identity_terminal_native_busy'):
                h.poll_items([{'device_id': 1, 'run_id': self.old, 'after': 0}])

    def test_poll_preserves_available_identity_metadata(self):
        proof = {'identity_terminal_handoff': {'schema': 'proof-fixture'}}
        with patch.object(h, 'STATE', self.root), patch.object(identities, 'poll', return_value=proof):
            item = h.poll_items([{'device_id': 1, 'run_id': self.old, 'after': 0}])['items'][0]
        self.assertEqual(item['identity_terminal_handoff'], proof['identity_terminal_handoff'])

    def test_skipping_optional_old_proof_does_not_bypass_device_ownership(self):
        with patch.object(identities, 'poll', side_effect=self.busy_old) as poll, \
             patch.object(h, 'install_unit'), patch.object(h, 'trusted_device', side_effect=RuntimeError('binding changed')), \
             patch.object(h, 'ctl') as control, patch.object(h.sys, 'stdin', io.StringIO(json.dumps(self.config))):
            with self.assertRaisesRegex(RuntimeError, 'binding changed'):
                h.manage('start', self.folder, 1, None)
        poll.assert_not_called()
        control.assert_not_called()
        with h.db(self.folder) as db:
            self.assertEqual(db.execute('SELECT id FROM runs WHERE current=1').fetchone()[0], self.old)
            self.assertIsNone(db.execute('SELECT id FROM runs WHERE id=?', (self.new,)).fetchone())


if __name__ == '__main__':
    unittest.main()
