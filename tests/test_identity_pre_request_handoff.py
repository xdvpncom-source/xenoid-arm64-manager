"""A pending receipt with only its owner cannot inherit a stale active request."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_identity_terminal_handoff as terminal_fixture


class PreRequestHandoffTests(unittest.TestCase):
    def setUp(self):
        terminal = terminal_fixture.TerminalHandoffTests()
        terminal.setUp()
        self.addCleanup(terminal.doCleanups)
        self.f, self.h, self.folder, self.db = terminal.f, terminal.h, terminal.folder, terminal.db
        self.terminal = terminal
        request = self.f.request
        fields = ('request_sha256', 'native_transaction', 'target_sha256', 'attempt', 'operation_key')
        self.h.write_once(self.f.request_path.with_suffix('.ack.json'),
                          {**{key: request[key] for key in fields}, 'decision': 'reserved'})
        state = {'schema': 'dev.xenoid.device-regenerate/v3', 'phase': 'committed',
                 'instanceId': request['binding']['instance_id'], 'transactionId': request['native_transaction'],
                 'before': request['native_before'], 'target': request['target']}
        self.h.write_once(self.f.gate.path / 'prepared.json', {'native_transaction': state['transactionId'],
                          'target_sha256': request['target_sha256'], 'attempt': 0})
        self.stale, self.stale_path = self.f.gate._request(0, 'finalize', state['before'], request['before_values'],
            state['target'], request['location_sidecar'],
            values=self.h.values_from_target(state['target'], request['location_sidecar']), state=state)
        self.h.write_once(self.stale_path.with_suffix('.ack.json'),
                          {**{key: self.stale[key] for key in fields}, 'decision': 'finalized'})
        self.committed = {**self.f.receipt, 'status': 'committed', 'native_journal': state,
            'native_completion': {'stable_digest': state['target']['stableDigest']},
            'result': {'ok': True, 'stable_digest_after': state['target']['stableDigest']}}
        self.key = 'run:iteration-13'
        self.plan = {'fixture': 'fixed-profile-plan'}
        self.context = {'scenario_sha256': self.stale['context']['scenario_sha256'],
                        'profile_plan_sha256': self.h.digest(self.plan)}
        self.gate = self.h.NativeGate(self.folder, self.f.old_config, self.f.fixture.manager,
                                      self.key, context=self.context)
        self.transaction = self.gate.transaction_id()
        self.pending = {'status': 'pending', 'binding': self.f.receipt['binding'],
            'before_digest': state['target']['stableDigest'], 'profile_plan': self.plan,
            'identity_reservation_context': self.context}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE regeneration_receipts SET document=?', (json.dumps(self.committed),))
            conn.execute('UPDATE runs SET iteration=13 WHERE id=?', (self.f.old,))
            conn.execute('INSERT INTO regeneration_receipts VALUES(?,?,?,?)',
                         (self.f.old, 13, self.key, json.dumps(self.pending)))

    def proof(self):
        return self.h.terminal_handoff(self.folder, self.f.old)

    def change_pending(self, **updates):
        document = {**self.pending, **updates}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE regeneration_receipts SET document=? WHERE iteration=13', (json.dumps(document),))

    def files(self):
        return {str(path.relative_to(self.folder)): path.read_bytes()
                for path in self.folder.rglob('*') if path.is_file()}

    def test_exact_owner_only_handoff_preserves_every_byte_and_target(self):
        before = self.files()
        proof = self.proof()
        self.assertEqual(proof['schema'], self.h.PRE_REQUEST_HANDOFF_SCHEMA)
        self.assertEqual(proof['original_iteration'], 13)
        self.assertEqual(proof['operation_key'], self.key)
        self.assertEqual(proof['request_sha256'], self.stale['request_sha256'])
        self.assertEqual(proof['pre_request']['active_iteration'], 7)
        self.assertEqual(proof['pre_request']['native_transaction'], self.transaction)
        self.assertEqual(proof['pre_request']['before_digest'], self.pending['before_digest'])
        self.assertIsNone(proof['native_journal_sha256'])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.proof(), proof)
        self.terminal.native_mock.assert_called_with(self.stale['binding'], None)

    def test_poll_never_relabels_stale_request_as_pending_operation(self):
        item = self.h.poll(self.folder, self.f.old)
        self.assertEqual(item['identity_request'], self.stale)
        self.assertEqual(item['identity_terminal_handoff']['operation_key'], self.key)
        self.assertNotEqual(item['identity_request']['operation_key'], self.key)

    def test_successor_before_first_reserve_emits_no_recovery_ack_or_old_request(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE runs SET current=0')
            conn.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                         (self.f.current, json.dumps(self.f.current_config), 'queued', 0, 1))
        before = self.files()
        self.assertEqual(self.h.poll(self.folder, self.f.current), {})
        self.assertEqual(self.files(), before)
        self.f.worker_mock.assert_not_called()

    def test_successor_pre_request_rejects_any_pending_native_journal(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE runs SET current=0')
            conn.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                         (self.f.current, json.dumps(self.f.current_config), 'queued', 0, 1))
        self.terminal.native_mock.side_effect = self.h.ReservationPending('identity_terminal_native_state_changed')
        before = self.files()
        with self.assertRaisesRegex(self.h.ReservationPending, 'native_state_changed'):
            self.h.poll(self.folder, self.f.current)
        self.assertEqual(self.files(), before)
        self.terminal.native_mock.assert_called_with(self.stale['binding'], None)

    def test_previous_reserve_schema_attempt_and_target_digest_are_pinned(self):
        path = self.f.request_path
        original = self.h.read(path)
        ack_path = path.with_suffix('.ack.json')
        for change in ({'schema': 'wrong'}, {'attempt': 1}, {'target_sha256': '0' * 64}):
            with self.subTest(change=change):
                changed = self.h.signed({**{key: value for key, value in original.items() if key != 'request_sha256'}, **change}, 'request_sha256')
                self.h.replace_owned(path, changed)
                ack = {key: changed[key] for key in ('request_sha256', 'native_transaction', 'target_sha256', 'attempt', 'operation_key')}
                self.h.replace_owned(ack_path, {**ack, 'decision': 'reserved'})
                with self.assertRaisesRegex(self.h.ReservationPending, 'previous_uncommitted'):
                    self.proof()

    def test_every_extra_operation_file_rejects_pre_request_classification(self):
        for name in ('prepared.json', 'attempt-0.reserve.json', 'attempt-0.reserve.ack.json', 'unexpected'):
            with self.subTest(name=name):
                path = self.gate.path / name
                self.h.write_once(path, {})
                with self.assertRaisesRegex(self.h.ReservationPending, 'already_started'):
                    self.proof()
                path.unlink()

    def test_owner_context_and_transaction_mismatch_rejected(self):
        path = self.gate.path / 'owner.json'
        original = self.h.read(path)
        for change in ({'context': {}}, {'run_id': '0' * 32}, {'native_transaction': self.stale['native_transaction']}, {'operation_key': 'wrong'}, {'binding': {}}):
            with self.subTest(change=change):
                self.h.replace_owned(path, {**original, **change})
                with self.assertRaises(self.h.ReservationPending):
                    self.proof()
        self.h.replace_owned(path, original)

    def test_started_native_or_preparation_stage_rejected(self):
        for key in ('native_journal', 'native_completion', 'result', 'completed_at', 'preparation'):
            with self.subTest(key=key):
                self.change_pending(**{key: {}})
                with self.assertRaisesRegex(self.h.ReservationPending, 'receipt_changed'):
                    self.proof()
        self.change_pending()

    def test_changed_before_or_profile_context_rejected(self):
        for change in ({'before_digest': '0' * 64}, {'profile_plan': {'changed': True}}, {'identity_reservation_context': {}}):
            self.change_pending(**change)
            with self.assertRaises(self.h.ReservationPending):
                self.proof()

    def test_previous_uncommitted_receipt_or_missing_final_ack_rejected(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE regeneration_receipts SET document=? WHERE iteration=7',
                         (json.dumps({**self.committed, 'status': 'failed'}),))
        with self.assertRaisesRegex(self.h.ReservationPending, 'previous_uncommitted'):
            self.proof()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE regeneration_receipts SET document=? WHERE iteration=7', (json.dumps(self.committed),))
        self.stale_path.with_suffix('.ack.json').unlink()
        with self.assertRaisesRegex(self.h.ReservationPending, 'previous_uncommitted'):
            self.proof()

    def test_owner_inode_replacement_between_checks_rejected(self):
        path = self.gate.path / 'owner.json'
        owner = self.h.read(path)
        def replace_during_check(*args):
            self.h.replace_owned(path, owner)
            return self.terminal.worker
        self.terminal.worker_mock.side_effect = replace_during_check
        with self.assertRaisesRegex(self.h.ReservationPending, 'state_changed'):
            self.proof()

    def test_active_worker_or_native_busy_propagates_without_writes(self):
        before = self.files()
        self.terminal.worker_mock.side_effect = self.h.ReservationPending('identity_terminal_worker_active')
        with self.assertRaisesRegex(self.h.ReservationPending, 'worker_active'):
            self.proof()
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
