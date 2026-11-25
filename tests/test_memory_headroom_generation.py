"""Generation recovery with real private files and isolated kernel collaborators."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_memory_headroom as base
q, CID = base.q, base.CID
BOOT='11111111-2222-4333-8444-555555555555'

class Generation(base.Headroom):
    def setUp(self):
        super().setUp()
        boot=self.proc/'sys/kernel/random/boot_id';boot.parent.mkdir(parents=True,exist_ok=True);boot.write_text(BOOT+'\n')
        def local_move(source_fd,source_name,target_fd,target_name):
            # Test-only emulation under this fixture's exclusive execution.
            try:os.stat(target_name,dir_fd=target_fd,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise FileExistsError(target_name)
            os.rename(source_name,target_name,src_dir_fd=source_fd,dst_dir_fd=target_fd)
        self.move=patch.object(q,'_memory_generation_noreplace',side_effect=local_move)
        self.move_mock=self.move.start();self.addCleanup(self.move.stop)
    def advance(self,*,remove_old=True):
        self.apply();self.legacy=self.state/(CID+'.json')
        self.old_legacy=self.legacy.read_bytes();self.old_legacy_inode=self.legacy.stat().st_ino
        self.old_ledger=(self.state/(CID+'.systemd.json')).read_bytes()
        old=self.proc/'123'
        if remove_old:(old/'stat').unlink()
        new=self.proc/'456';new.mkdir();(new/'stat').write_text('456 (init) '+' '.join(['S']+['0']*18+['88888'])+'\n')
        (new/'cgroup').write_text('0::/system.slice/docker-'+CID+'.scope\n')
        self.owned['State'].update(Pid=456,StartedAt='2026-01-01T00:01:00Z')
        self.systemd_high='infinity';(self.folder/'memory.high').write_text('max\n')
        self.calls_before=len(self.systemd_calls)
    def owners(self):return list((self.state/(CID+'.generation-transitions')).glob('*.json'))
    def assert_legacy_exact(self):
        self.assertEqual(self.legacy.read_bytes(),self.old_legacy)
        self.assertEqual(self.legacy.stat().st_ino,self.old_legacy_inode)
    def assert_archived(self):
        archives=list((self.state/'generation-archive'/CID).glob('*/*.json'))
        self.assertEqual(len(archives),1);self.assertEqual(archives[0].read_bytes(),self.old_ledger)
        self.assertEqual(json.loads(self.owners()[0].read_text())['phase'],'archive_committed')
        self.assert_legacy_exact()
    def test_generation_normal_archive_and_new_ledger(self):
        self.advance();self.assertTrue(self.apply()['ok']);self.assert_archived()
        ledger=json.loads((self.state/(CID+'.systemd.json')).read_text())
        self.assertEqual(ledger['pid'],456);self.assertEqual(ledger['host_boot_id'],BOOT)
        self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_same_generation_no_archive_or_owner_write(self):
        self.apply();p=self.state/(CID+'.systemd.json');raw=p.read_bytes();inode=p.stat().st_ino
        self.assertFalse(self.apply()['changed']);self.assertEqual(p.read_bytes(),raw)
        self.assertEqual(p.stat().st_ino,inode);self.assertFalse((self.state/'generation-archive').exists())
    def test_generation_pending_old_ack_never_adopted_even_matching_values(self):
        self.advance();p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d['phase']='pending';q._memory_record(p,d)
        self.systemd_high=str(q.HIGH_BYTES);(self.folder/'memory.high').write_text(str(q.HIGH_BYTES)+'\n')
        raw=p.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'pending_old_ack'):self.apply()
        self.assertEqual(p.read_bytes(),raw);self.assertEqual(len(self.systemd_calls),self.calls_before)
        self.assertFalse((self.state/'generation-archive').exists())
    def test_generation_old_init_still_present_holds(self):
        self.advance(remove_old=False)
        with self.assertRaisesRegex(RuntimeError,'old_init_still_present'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_reused_old_pid_with_other_start_ticks_not_signalled(self):
        self.advance(remove_old=False);(self.proc/'123/stat').write_text('123 (unrelated) '+' '.join(['S']+['0']*18+['99999'])+'\n')
        with patch.object(q.os,'kill',side_effect=AssertionError('must not signal reusedPID')):
            self.apply()
        self.assert_archived()
    def test_generation_known_host_reboot_committed_owner_archives(self):
        self.advance();(self.proc/'sys/kernel/random/boot_id').write_text('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n')
        with patch.object(q.os,'kill',side_effect=AssertionError('no signal after reboot')):self.apply()
        self.assert_archived();self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_unrecorded_boot_holds_new_generation(self):
        self.advance();p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d.pop('host_boot_id');q._memory_record(p,d)
        with self.assertRaisesRegex(RuntimeError,'old_boot_unknown'):self.apply()
    def test_generation_same_current645_committed_owner_can_enrich_boot(self):
        self.apply();p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d.pop('host_boot_id');q._memory_record(p,d)
        scalar=(self.state/(CID+'.json')).read_bytes();self.apply()
        self.assertEqual(json.loads(p.read_text())['host_boot_id'],BOOT)
        self.assertEqual((self.state/(CID+'.json')).read_bytes(),scalar)
    def test_generation_noninfinity_new_unowned_scope_holds(self):
        self.advance();self.systemd_high=str(q.HIGH_BYTES)
        with self.assertRaisesRegex(RuntimeError,'unowned_new_unit_high'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_inspect_never_creates_intent_or_moves(self):
        self.advance();raw=(self.state/(CID+'.systemd.json')).read_bytes()
        with self.assertRaisesRegex(RuntimeError,'transition_requires_apply'):self.apply(operation='inspect')
        self.assertEqual((self.state/(CID+'.systemd.json')).read_bytes(),raw)
        self.assertFalse((self.state/(CID+'.generation-transitions')).exists())
        self.assertEqual(self.move_mock.call_count,0)
    def test_generation_crash_before_move_recovers_one_move_and_setter(self):
        self.advance();actual=q._memory_generation_noreplace
        with patch.object(q,'_memory_generation_noreplace',side_effect=RuntimeError('crash_before_move')):
            with self.assertRaisesRegex(RuntimeError,'crash_before_move'):self.apply()
        self.assertEqual(json.loads(self.owners()[0].read_text())['phase'],'pending_archive')
        self.assertEqual(len(self.systemd_calls),self.calls_before);self.apply();self.assert_archived()
        self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_crash_after_move_before_fsync_recovers_no_duplicate_move(self):
        self.advance();actual=q._memory_generation_noreplace
        def crash(*args):actual(*args);raise RuntimeError('crash_after_move')
        with patch.object(q,'_memory_generation_noreplace',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'crash_after_move'):self.apply()
        moved=self.move_mock.call_count;self.apply();self.assertEqual(self.move_mock.call_count,moved)
        self.assert_archived();self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_crash_after_archive_commit_before_apply_recovers(self):
        self.advance();actual=q._memory_scope_sync
        with patch.object(q,'_memory_scope_sync',side_effect=RuntimeError('crash_before_apply')):
            with self.assertRaisesRegex(RuntimeError,'crash_before_apply'):self.apply()
        self.assert_archived();self.assertEqual(len(self.systemd_calls),self.calls_before)
        self.apply();self.assert_archived();self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_runtime_changed_during_pending_archive_holds_new_entry(self):
        self.advance()
        with patch.object(q,'_memory_generation_noreplace',side_effect=RuntimeError('pause')):
            with self.assertRaisesRegex(RuntimeError,'pause'):self.apply()
        self.owned['State']['StartedAt']='2026-01-01T00:02:00Z'
        with self.assertRaisesRegex(RuntimeError,'prior_transition_pending_or_unknown'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_lost_new_setter_ack_reconciles_without_second_setter(self):
        self.advance();actual=q._memory_write
        def accepted(*args,**kwargs):actual(*args,**kwargs);raise RuntimeError('unknown_ack')
        with patch.object(q,'_memory_write',side_effect=accepted):
            with self.assertRaisesRegex(RuntimeError,'unknown_ack'):self.apply()
        self.apply();self.assert_archived();self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_missing_archive_or_active_replacement_holds(self):
        self.advance()
        with patch.object(q,'_memory_generation_noreplace',side_effect=RuntimeError('pause')):
            with self.assertRaisesRegex(RuntimeError,'pause'):self.apply()
        p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d['foreign_admin_marker']=True;q._memory_record(p,d)
        raw=p.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'archive_conflict'):self.apply()
        self.assertEqual(p.read_bytes(),raw);self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_two_successive_runtime_generations_archive_each(self):
        self.advance();self.apply();self.assert_archived()
        (self.proc/'456/stat').write_text('456 (unrelated) '+' '.join(['S']+['0']*18+['99999'])+'\n')
        new=self.proc/'789';new.mkdir();(new/'stat').write_text('789 (init) '+' '.join(['S']+['0']*18+['111111'])+'\n')
        (new/'cgroup').write_text('0::/system.slice/docker-'+CID+'.scope\n')
        self.owned['State'].update(Pid=789,StartedAt='2026-01-01T00:02:00Z')
        self.systemd_high='infinity';(self.folder/'memory.high').write_text('max\n');self.apply()
        self.assertEqual(len(list((self.state/'generation-archive'/CID).glob('*/*.json'))),2)
        self.assertEqual(len(self.owners()),2);self.assert_legacy_exact()
    def test_generation_index_holds_changed_runtime_after_move_before_commit(self):
        self.advance();actual=q._memory_generation_noreplace
        def crash(*args):actual(*args);raise RuntimeError('after_move')
        with patch.object(q,'_memory_generation_noreplace',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'after_move'):self.apply()
        self.owned['State']['StartedAt']='2026-01-01T00:03:00Z'
        with self.assertRaisesRegex(RuntimeError,'prior_transition_pending_or_unknown'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_unknown_ack_old_missing_active_ledger_cannot_skip_disabled(self):
        self.advance();actual=q._memory_generation_noreplace
        def crash(*args):actual(*args);raise RuntimeError('after_move')
        with patch.object(q,'_memory_generation_noreplace',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'after_move'):self.apply()
        # Even without either active ledger, the transition directory prevents
        # the old disabled/no-owner fast path from bypassing its pending fence.
        (self.state/(CID+'.json')).unlink()
        with self.assertRaisesRegex(RuntimeError,'legacy_changed'):self.apply(reserve_mib=0)
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_committed_restored_old_owner_archives(self):
        self.apply();self.apply(operation='rollback');self.advance()
        # advance reapplies its current generation, so explicitly mark a valid
        # committed restore before creating the next generation.
        p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d.update(phase='restored',desired_unit_high='infinity',desired_raw_high='max');q._memory_record(p,d)
        self.old_ledger=p.read_bytes();self.apply();self.assert_archived()
    def test_generation_transition_parent_symlink_holds(self):
        self.advance();outside=self.root/'outside-private';outside.mkdir(mode=0o700)
        (self.state/(CID+'.generation-transitions')).symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):self.apply()
        self.assertEqual(list(outside.iterdir()),[]);self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_existing_archive_is_never_overwritten(self):
        self.advance()
        with patch.object(q,'_memory_generation_noreplace',side_effect=RuntimeError('pause')):
            with self.assertRaisesRegex(RuntimeError,'pause'):self.apply()
        intent=json.loads(self.owners()[0].read_text());archive=self.state.joinpath(*intent['archive_parts'])
        archive.parent.mkdir(parents=True,mode=0o700,exist_ok=True);archive.write_bytes(b'foreign');archive.chmod(0o600)
        with self.assertRaises((RuntimeError,json.JSONDecodeError)):self.apply()
        self.assertEqual(archive.read_bytes(),b'foreign');self.assertEqual(len(self.systemd_calls),self.calls_before)
    @unittest.skipUnless(__import__('sys').platform=='linux','Actual renameat2 requires Linux')
    def test_generation_actual_noreplace_refuses_existing_target(self):
        self.move.stop();self.state.mkdir(mode=0o700)
        source=self.state/'source';target=self.state/'target';source.write_bytes(b'owned');target.write_bytes(b'foreign')
        fd=os.open(self.state,os.O_RDONLY|os.O_DIRECTORY)
        try:
            with self.assertRaises(FileExistsError):q._memory_generation_noreplace(fd,'source',fd,'target')
            self.assertEqual(source.read_bytes(),b'owned');self.assertEqual(target.read_bytes(),b'foreign')
            target.unlink();q._memory_generation_noreplace(fd,'source',fd,'target')
            self.assertFalse(source.exists());self.assertEqual(target.read_bytes(),b'owned')
        finally:os.close(fd)
    def test_generation_new_boot_ignores_reused_old_pid_without_read_or_signal(self):
        self.advance(remove_old=False);(self.proc/'sys/kernel/random/boot_id').write_text('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n')
        actual=Path.read_text
        def read(path,*args,**kwargs):
            if path==self.proc/'123/stat':raise AssertionError('old PID irrelevant on new boot')
            return actual(path,*args,**kwargs)
        with patch.object(q.Path,'read_text',read),patch.object(q.os,'kill',side_effect=AssertionError('no signals')):
            self.apply()
        self.assert_archived();self.assertEqual(len(self.systemd_calls)-self.calls_before,1)
    def test_generation_malformed_old_boot_holds(self):
        self.advance();p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d['host_boot_id']='invalid';q._memory_record(p,d)
        with self.assertRaisesRegex(RuntimeError,'old_boot_unknown'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_new_boot_requires_pristine_kernel_high(self):
        self.advance();(self.proc/'sys/kernel/random/boot_id').write_text('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n')
        (self.folder/'memory.high').write_text(str(q.HIGH_BYTES)+'\n')
        with self.assertRaisesRegex(RuntimeError,'new_boot_raw_not_pristine'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_new_boot_old_pending_ack_never_archived(self):
        self.advance();(self.proc/'sys/kernel/random/boot_id').write_text('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n')
        p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text());d['phase']='pending';q._memory_record(p,d)
        with self.assertRaisesRegex(RuntimeError,'pending_old_ack'):self.apply()
        self.assertEqual(len(self.systemd_calls),self.calls_before)
    def test_generation_rollback_without_setter_still_owns_new_restored_generation(self):
        self.advance();self.apply(operation='rollback')
        archives=list((self.state/'generation-archive'/CID).glob('*/*.json'))
        self.assertEqual(len(archives),1);self.assertEqual(archives[0].read_bytes(),self.old_ledger)
        self.assertEqual(json.loads(self.owners()[0].read_text())['phase'],'archive_committed')
        p=self.state/(CID+'.systemd.json');d=json.loads(p.read_text())
        self.assertEqual(d['pid'],456);self.assertEqual(d['phase'],'restored')
        self.assertEqual(d['desired_raw_high'],'max');self.assertEqual(len(self.systemd_calls),self.calls_before)
        # A subsequent runtime must be able to archive the committed restore.
        (self.proc/'456/stat').unlink();new=self.proc/'789';new.mkdir()
        (new/'stat').write_text('789 (init) '+' '.join(['S']+['0']*18+['111111'])+'\n')
        (new/'cgroup').write_text('0::/system.slice/docker-'+CID+'.scope\n')
        self.owned['State'].update(Pid=789,StartedAt='2026-01-01T00:02:00Z')
        self.apply();self.assertEqual(len(list((self.state/'generation-archive'/CID).glob('*/*.json'))),2)

if __name__=='__main__':unittest.main(verbosity=2)
