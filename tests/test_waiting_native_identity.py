"""Fresh readback beside a verified pre-journal reservation waiter."""
import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('idle_fixture', HERE / 'test_idle_native_identity.py')
idle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idle)
host, identity = idle.host, idle.identity


class WaitingNativeIdentity(unittest.TestCase):
    def setUp(self):
        self.fixture = idle.IdleNativeIdentity()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.target = idle.binding()
        self.run = 'd' * 32
        self.worker = 9001
        self.folder = self.root / 'runs' / '1'
        self.folder.mkdir(parents=True, mode=0o700)
        self.root_run = self.folder / 'identity-reservations' / self.run
        self.root_run.mkdir(parents=True, mode=0o700)
        self.root_run.parent.chmod(0o700)
        self.key = 'vpn-cycle:1'
        self.operation = self.root_run / hashlib.sha256(self.key.encode()).hexdigest()
        self.operation.mkdir(mode=0o700)
        self.context = {'scenario_sha256': hashlib.sha256(b'test scenario').hexdigest(), 'profile_plan_sha256': None}
        self.request = identity.signed({'schema': identity.REQUEST_SCHEMA, 'binding': self.target,
            'run_id': self.run, 'operation_key': self.key, 'attempt': 0, 'native_transaction': 'e' * 32,
            'phase': 'reserve', 'before_values': dict(idle.VALUES), 'native_before': {'stableDigest': 'f' * 64},
            'context': self.context, 'values': None, 'native_state': None}, 'request_sha256')
        self.request_path = self.operation / 'attempt-0.reserve.json'
        self.write(self.request_path, self.request)
        self.write(self.root_run / 'active.json', {'path': str(self.request_path.relative_to(self.root_run)),
                                                'sha256': self.request['request_sha256']})
        self.write(self.operation / 'owner.json', {key: self.request[key] for key in
                   ('binding', 'run_id', 'operation_key', 'native_transaction', 'context')})
        self.receipt = {'status': 'pending', 'binding': {key: self.target[key] for key in ('instance_id', 'resource_tag', 'container_id')},
                        'identity_reservation_context': self.context, 'before_digest': 'f' * 64}
        self.database = self.folder / 'runs.db'
        with closing(sqlite3.connect(str(self.database))) as conn, conn:
            conn.executescript('CREATE TABLE runs(id,config,state,iteration,current); '
                               'CREATE TABLE regeneration_receipts(run_id,iteration,operation_key,document);')
            self.config = {'run_id': self.run, 'identity_reservations': True, 'source': 'test scenario',
                           'instance_id': self.target['instance_id'], 'resource_tag': self.target['resource_tag'],
                           'expected_container_id': self.target['container_id']}
            conn.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (self.run, json.dumps(self.config), 'running', 1, 1))
            conn.execute('INSERT INTO regeneration_receipts VALUES(?,?,?,?)',
                         (self.run, 1, self.key, json.dumps(self.receipt)))
        self.database.chmod(0o600)
        self.process = self.root / str(self.worker)
        self.process.mkdir()
        (self.process / 'fd').mkdir()
        (self.process / 'fd' / '10').symlink_to(self.fixture.state / 'operation.lock')
        (self.process / 'status').write_text('Name:\tpython3\nUid:\t0\t0\t0\t0\n')
        self.argv = ['/usr/bin/python3', str(host.HERE / 'script_host.py'), 'worker', '1']
        (self.process / 'cmdline').write_bytes(('\0'.join(self.argv) + '\0').encode())
        self.stat = f'{self.worker} (python3) ' + ' '.join(['S'] + ['0'] * 18 + ['567'] + ['0'] * 5)
        (self.process / 'stat').write_text(self.stat)
        self.group = '/system.slice/system-xenoid\\x2dpanel\\x2dscript.slice/xenoid-panel-script@1.service'
        (self.process / 'cgroup').write_text('0::' + self.group + '\n')
        lock_info = (self.fixture.state / 'operation.lock').stat()
        self.lock_line = f'49: FLOCK ADVISORY WRITE {self.worker} {os.major(lock_info.st_dev):02x}:{os.minor(lock_info.st_dev):02x}:{lock_info.st_ino} 0 EOF\n'
        (self.root / 'locks').write_text(self.lock_line + self.lock_line.replace('49: ', '49: -> ').replace(str(self.worker), '9999', 1))
        self.systemd = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': str(self.worker), 'ControlGroup': self.group}
        self.original_stat, self.original_fstat = Path.stat, os.fstat
        # Emulate only root ownership; modes, contents, SQLite and flock are real.
        def root_info(info):
            result = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            result['st_uid'] = 0
            return types.SimpleNamespace(**result)
        def lstat(path):
            return root_info(os.lstat(path))
        def stat(path, **kwargs):
            return root_info(self.original_stat(path, **kwargs))
        self.patches = [patch.object(host, 'STATE', self.root / 'runs'), patch.object(Path, 'lstat', lstat),
                        patch.object(Path, 'stat', stat), patch.object(os, 'fstat', side_effect=lambda fd: root_info(self.original_fstat(fd))),
                        patch.object(host.subprocess, 'run', side_effect=self.show)]
        for item in self.patches: item.start()
        self.lock_stream = (self.fixture.state / 'operation.lock').open()
        idle.fcntl.flock(self.lock_stream, idle.fcntl.LOCK_EX)
        self.before_bytes = {p: p.read_bytes() for p in (self.database, self.request_path, self.root_run / 'active.json')}

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def show(self, args, **kwargs):
        self.assertEqual(args[:3], ['systemctl', 'show', 'xenoid-panel-script@1.service'])
        self.assertLessEqual(kwargs['timeout'], 8)
        return types.SimpleNamespace(returncode=0, stdout=('\n'.join(k + '=' + v for k, v in self.systemd.items())).encode())

    def tearDown(self):
        self.lock_stream.close()
        for item in reversed(self.patches): item.stop()
        self.fixture.tearDown()

    def observe(self): return self.fixture.observe()

    def assert_rejected(self):
        answer = self.observe()
        self.assertNotEqual(answer['status'], 'observed')
        self.assertNotIn('observation', answer)
        return answer

    def update_receipt(self, **values):
        self.receipt.update(values)
        with closing(sqlite3.connect(str(self.database))) as conn, conn:
            conn.execute('UPDATE regeneration_receipts SET document=?', (json.dumps(self.receipt),))

    def test_verified_waiter_reads_actual_values_and_preserves_all_files(self):
        with patch.object(host.time, 'time', return_value=2345678901.25): result = self.observe()
        self.assertEqual(result['status'], 'observed', result)
        proof = result['observation']
        self.assertEqual(proof['values'], idle.VALUES)
        self.assertEqual(proof['captured_at'], 2345678901.25)
        self.assertEqual(proof['request_id'], 'b' * 32)
        self.assertEqual(identity.verified(proof, 'proof_sha256'), proof)
        self.assertTrue(self.fixture.timeouts, 'Must read Android, not reuse before_values')
        self.assertTrue(all(0 < x <= 8 for x in self.fixture.timeouts))
        self.assertEqual({p: p.read_bytes() for p in self.before_bytes}, self.before_bytes)

    def test_unknown_busy_owner_stays_native_busy(self):
        (self.root / 'locks').write_text(self.lock_line.replace(str(self.worker), '9999', 1))
        self.assertEqual(self.assert_rejected()['reason'], 'native_busy')
        self.assertFalse(self.fixture.timeouts)

    def test_blocked_waiter_is_not_lock_owner(self):
        (self.root / 'locks').write_text(self.lock_line.replace('49: ', '49: -> '))
        self.assert_rejected()

    def test_expected_worker_must_have_open_native_inode(self):
        (self.process / 'fd' / '10').unlink(); self.assert_rejected()

    def test_wrong_worker_command_is_rejected(self):
        (self.process / 'cmdline').write_bytes(b'/usr/bin/python3\0unrelated.py\0worker\01\0')
        self.assert_rejected()

    def test_different_service_cgroup_is_rejected(self):
        (self.process / 'cgroup').write_text('0::/system.slice/other.service\n'); self.assert_rejected()

    def test_current_run_cannot_adopt_old_request(self):
        with closing(sqlite3.connect(str(self.database))) as conn, conn:
            conn.execute('UPDATE runs SET id=?', ('c' * 32,))
        self.assert_rejected()

    def test_existing_ack_is_rejected_without_read(self):
        self.write(self.request_path.with_suffix('.ack.json'), {})
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_existing_prepared_is_rejected_without_read(self):
        self.write(self.operation / 'prepared.json', {})
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_pending_native_completion_is_not_a_reserve_wait(self):
        self.update_receipt(native_completion={}); self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_receipt_native_journal_is_not_a_reserve_wait(self):
        self.update_receipt(native_journal={}); self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_live_native_journal_is_rejected_without_read(self):
        (self.fixture.state / 'device-regenerate-v3.json').touch()
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def mutate_during_read(self, mutation):
        original = identity.observe
        def changed(reader):
            result = original(reader); mutation(); return result
        with patch.object(identity, 'observe', side_effect=changed): answer = self.assert_rejected()
        self.assertEqual(answer['status'], 'unavailable')

    def test_ack_arriving_during_read_discards_observation(self):
        self.mutate_during_read(lambda: self.write(self.request_path.with_suffix('.ack.json'), {}))

    def test_journal_appearing_during_read_discards_observation(self):
        self.mutate_during_read(lambda: (self.fixture.state / 'device-regenerate-v3.json').touch())

    def test_active_request_switch_during_read_discards_observation(self):
        self.mutate_during_read(lambda: self.write(self.root_run / 'active.json', {'path': 'invalid', 'sha256': 'c' * 64}))

    def test_worker_pid_reuse_during_read_discards_observation(self):
        self.mutate_during_read(lambda: (self.process / 'stat').write_text(self.stat.replace('567', '568')))

    def test_worker_lock_owner_change_during_read_discards_observation(self):
        self.mutate_during_read(lambda: (self.root / 'locks').write_text(self.lock_line.replace(str(self.worker), '9999', 1)))

    def test_actual_identity_drift_is_not_published_as_old_baseline(self):
        original = identity.observe
        def drift(reader):
            values, build = original(reader); return {**values, 'serial': 'CHANGED'}, build
        with patch.object(identity, 'observe', side_effect=drift): self.assert_rejected()

    def test_fresh_read_and_both_guards_share_one_deadline(self):
        clock = [0.0]
        original = self.fixture.manager.docker_exec
        def slow(*args, **kwargs):
            result = original(*args, **kwargs); clock[0] += 9; return result
        self.fixture.manager.docker_exec = slow
        with patch.object(host.time, 'monotonic', side_effect=lambda: clock[0]): self.assert_rejected()

    def test_wrong_source_context_is_rejected(self):
        self.config['source'] = 'different scenario'
        with closing(sqlite3.connect(str(self.database))) as conn, conn:
            conn.execute('UPDATE runs SET config=?', (json.dumps(self.config),))
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_finalization_request_is_not_a_reserve_wait(self):
        value = dict(self.request); value['phase'] = 'finalize'
        value = identity.signed({k: v for k, v in value.items() if k != 'request_sha256'}, 'request_sha256')
        self.write(self.request_path, value)
        self.write(self.root_run / 'active.json', {'path': str(self.request_path.relative_to(self.root_run)),
                                                'sha256': value['request_sha256']})
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_private_request_file_mode_is_required(self):
        self.request_path.chmod(0o644)
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_request_digest_is_not_trusted_without_verification(self):
        self.write(self.request_path, {**self.request, 'before_values': {**idle.VALUES, 'serial': 'different'}})
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_receipt_must_belong_to_current_container(self):
        self.update_receipt(binding={**self.receipt['binding'], 'container_id': 'c' * 64})
        self.assert_rejected(); self.assertFalse(self.fixture.timeouts)

    def test_owner_switch_during_read_is_rejected(self):
        owner = json.loads((self.operation / 'owner.json').read_text())
        self.mutate_during_read(lambda: self.write(self.operation / 'owner.json', {**owner, 'native_transaction': 'a' * 32}))

    def test_container_replacement_during_read_is_rejected(self):
        self.mutate_during_read(lambda: self.fixture.row.update(Id='c' * 64))

    def test_prepared_appearing_during_read_is_rejected(self):
        self.mutate_during_read(lambda: self.write(self.operation / 'prepared.json', {}))

    def test_systemd_worker_change_during_read_is_rejected(self):
        self.mutate_during_read(lambda: self.systemd.update(MainPID='9002'))

    @unittest.skipUnless(Path('/proc/locks').is_file(), 'requires Linux kernel flock records')
    def test_kernel_flock_owner_and_open_descriptor_are_both_required(self):
        # Keep process/unit metadata deterministic, but obtain the lock owner and
        # open FD from the real Linux kernel instead of synthesizing those proofs.
        idle.fcntl.flock(self.lock_stream, idle.fcntl.LOCK_UN)
        command = ('import fcntl,sys; f=open(sys.argv[1]); '
                   'fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()')
        child = subprocess.Popen(['/usr/bin/python3', '-c', command, str(self.fixture.state / 'operation.lock')],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'ready\n')
            new_process = self.root / str(child.pid)
            self.process.rename(new_process); self.process = new_process
            self.systemd['MainPID'] = str(child.pid)
            (self.process / 'fd' / '10').unlink(); (self.process / 'fd').rmdir()
            (self.process / 'fd').symlink_to(Path('/proc') / str(child.pid) / 'fd')
            (self.root / 'locks').unlink(); (self.root / 'locks').symlink_to('/proc/locks')
            result = self.observe()
            self.assertEqual(result['status'], 'observed', result)
            self.systemd['MainPID'] = str(os.getpid())
            self.assert_rejected()
        finally:
            child.communicate(timeout=5)


if __name__ == '__main__': unittest.main()
