"""Behavioral checks for the producer fix, host migration and boot binding."""
import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import types
import sys
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/'qa/qa_host_memory.py'
spec=importlib.util.spec_from_file_location('host_vm_isolation',PATH)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ProducerIsolation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'system/bin').mkdir(parents=True)
        self.target=self.root/m.SCRIPT;self.backup=self.root/'originals/source.json'
        self.stock=b'#!/bin/sh\necho stock producer\n'
        self.target.write_bytes(self.stock);self.target.chmod(0o755)
        self.sha=patch.object(m,'STOCK_SHA256',m.digest(self.stock));self.sha.start();self.addCleanup(self.sha.stop)
        self.xattr_writes=[]
        for name,value in (('listxattr',lambda path:['security.selinux']),
                           ('getxattr',lambda path,key:b'u:object_r:system_file:s0'),
                           ('setxattr',lambda fd,key,value:self.xattr_writes.append((key,value)))):
            context=patch.object(m.os,name,side_effect=value,create=True)
            context.start();self.addCleanup(context.stop)

    def apply(self):return m.patch_producer(self.root,self.backup,required_owner=os.getuid())

    def test_known_source_is_atomic_backed_up_and_idempotent(self):
        original=self.target.stat();result=self.apply()
        self.assertTrue(result['changed']);self.assertEqual(self.target.read_bytes(),m.GUARD)
        self.assertNotEqual(self.target.stat().st_ino,original.st_ino)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode),0o755)
        self.assertEqual(self.xattr_writes,[('security.selinux',b'u:object_r:system_file:s0')])
        saved=json.loads(self.backup.read_text());self.assertEqual(base64.b64decode(saved['body_base64']),self.stock)
        before=self.backup.stat().st_mtime_ns
        self.assertFalse(self.apply()['changed']);self.assertEqual(self.backup.stat().st_mtime_ns,before)
        self.assertFalse(list((self.root/'system/bin').glob('.xenoid-host-vm-*')))

    def test_unknown_source_does_not_mutate_or_create_backup(self):
        self.target.write_bytes(b'custom administrator script')
        with self.assertRaisesRegex(RuntimeError,'unknown_producer'):self.apply()
        self.assertEqual(self.target.read_bytes(),b'custom administrator script');self.assertFalse(self.backup.exists())

    def test_symlink_and_extra_hardlink_are_rejected(self):
        self.target.unlink();self.target.symlink_to(self.root/'elsewhere')
        with self.assertRaisesRegex(RuntimeError,'metadata_invalid'):self.apply()
        self.target.unlink();self.target.write_bytes(self.stock);self.target.chmod(0o755)
        os.link(self.target,self.root/'second-name')
        with self.assertRaisesRegex(RuntimeError,'metadata_invalid'):self.apply()

    def test_writable_or_symlinked_parent_is_rejected(self):
        (self.root/'system/bin').chmod(0o777)
        with self.assertRaisesRegex(RuntimeError,'parent_invalid'):self.apply()
        self.assertEqual(self.target.read_bytes(),self.stock)

    def test_conflicting_backup_prevents_replacement(self):
        self.backup.parent.mkdir();self.backup.write_text(json.dumps({'sha256':'a'*64,'body_base64':''}))
        with self.assertRaisesRegex(RuntimeError,'backup_conflict'):self.apply()
        self.assertEqual(self.target.read_bytes(),self.stock)

    def test_rename_failure_keeps_original_and_retains_backup(self):
        real_replace=m.os.replace
        def replace(a,b):
            if Path(b)==self.target:raise OSError('injected filesystem failure')
            return real_replace(a,b)
        with patch.object(m.os,'replace',side_effect=replace),self.assertRaises(OSError):self.apply()
        self.assertEqual(self.target.read_bytes(),self.stock);self.assertTrue(self.backup.exists())
        self.assertFalse(list((self.root/'system/bin').glob('.xenoid-host-vm-*')))

    def test_guard_only_records_numeric_request_and_never_writes_kernel(self):
        directory=self.root/'guest-data'
        script=m.GUARD.decode().replace('directory=/data/local/tmp/xenoid-host-vm','directory='+shlex.quote(str(directory)))
        result=subprocess.run(['/bin/sh','-s','--','834'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((directory/'extra_free_kbytes.requested').read_text(),'834\n')
        for args in ([],['-1'],['1; id'],['1','2'],['']):
            r=subprocess.run(['/bin/sh','-s','--',*args],input=script,text=True,capture_output=True)
            self.assertNotEqual(r.returncode,0)
        self.assertNotIn('> /proc/',script);self.assertNotIn('sysctl ',script)

    def test_offline_rollback_restores_exact_original_bytes_metadata_and_attrs(self):
        self.apply();saved=json.loads(self.backup.read_text())
        restored=m.restore_producer(self.root,self.backup,required_owner=os.getuid())
        self.assertTrue(restored['changed']);self.assertEqual(self.target.read_bytes(),self.stock)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode),saved['mode'])
        self.assertEqual(self.target.stat().st_gid,saved['gid'])
        self.assertEqual(self.xattr_writes[-1],('security.selinux',b'u:object_r:system_file:s0'))
        self.assertFalse(m.restore_producer(self.root,self.backup,required_owner=os.getuid())['changed'])

    def test_rollback_missing_backup_or_unknown_source_never_replaces_file(self):
        self.target.write_bytes(m.GUARD)
        with self.assertRaisesRegex(RuntimeError,'backup_missing'):
            m.restore_producer(self.root,self.backup,required_owner=os.getuid())
        self.assertEqual(self.target.read_bytes(),m.GUARD)
        self.target.write_bytes(b'custom')
        with self.assertRaisesRegex(RuntimeError,'unknown_producer'):
            m.restore_producer(self.root,self.backup,required_owner=os.getuid())
        self.assertEqual(self.target.read_bytes(),b'custom')


class HostValueOwnership(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.cfg=patch.object(m,'CONFIG',self.root/'qa-host-vm.json');self.cfg.start();self.addCleanup(self.cfg.stop)

    def test_no_admin_override_uses_kernel_default(self):
        self.assertEqual(m.configured_watermark([])[0],10)

    def test_admin_sysctl_order_and_comments_are_preserved(self):
        a=self.root/'a.conf';b=self.root/'b.conf'
        a.write_text('# vm.watermark_scale_factor=1000\nvm.watermark_scale_factor = 20\n')
        b.write_text('vm/watermark_scale_factor=35 # intentional host policy\n')
        self.assertEqual(m.configured_watermark([a,b]),(35,str(b)))

    def test_explicit_profile_wins_and_invalid_values_fail(self):
        m.CONFIG.write_text(json.dumps({'watermark_scale_factor':40}))
        self.assertEqual(m.configured_watermark([]),(40,'qa-host-vm.json'))
        for value in (True,0,-1,3001,'10'):
            m.CONFIG.write_text(json.dumps({'watermark_scale_factor':value}))
            with self.assertRaisesRegex(RuntimeError,'config_invalid'):m.configured_watermark([])

    def test_no_running_guarded_guest_never_changes_host(self):
        value=self.root/'kernel-knob';value.write_text('2965\n')
        with patch.object(m,'WATERMARK',value),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'host_lock',contextlib.nullcontext),patch.object(m,'require_registered_managers'):
            with self.assertRaisesRegex(RuntimeError,'no_guarded_running'):m.restore_host_watermark([])
        self.assertEqual(value.read_text(),'2965\n')


class BootFences(unittest.TestCase):
    def test_running_container_is_never_mounted_offline(self):
        with self.assertRaisesRegex(RuntimeError,'offline_guest_running'):
            m.offline_image(types.SimpleNamespace(),{'State':{'Running':True}})

    def test_changed_container_rejected_before_guard_mutation(self):
        manager=types.SimpleNamespace(_owned_container_record=lambda **k:({'Id':'b'*64},None))
        with self.assertRaisesRegex(RuntimeError,'container_binding_changed'):m.checked_container(manager,'a'*64)

    def test_invalid_offline_instance_id_rejected_before_filesystem(self):
        with self.assertRaisesRegex(RuntimeError,'instance_id_invalid'):
            m.offline_child({'instance_id':'../../not-an-instance'})


class RollbackTransaction(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.value=self.root/'kernel-knob';self.value.write_text('2965\n')
        for name,value in (('STATE',self.root/'state'),('CONFIG',self.root/'config.json'),('WATERMARK',self.value)):
            context=patch.object(m,name,value);context.start();self.addCleanup(context.stop)
        for name,value in (('geteuid',lambda:0),):
            context=patch.object(m.os,name,side_effect=value);context.start();self.addCleanup(context.stop)
        context=patch.object(m,'host_lock',contextlib.nullcontext);context.start();self.addCleanup(context.stop)
        self.managers=[]
        for n in (1,2):
            row={'Id':str(n)*64,'State':{'Running':False}}
            self.managers.append(types.SimpleNamespace(context=types.SimpleNamespace(instance_name='phone-'+str(n)),
                _owned_container_record=lambda row=row,**k:(row,None),
                instance_operation_lock=lambda **k:contextlib.nullcontext()))
        fake=types.ModuleType('qa_resources');fake.registry_names=lambda:['phone-1','phone-2']
        context=patch.dict(sys.modules,{'qa_resources':fake});context.start();self.addCleanup(context.stop)
        inactive=types.SimpleNamespace(returncode=0,stdout='LoadState=loaded\nMainPID=0\nActiveState=inactive\n')
        context=patch.object(m.subprocess,'run',return_value=inactive);context.start();self.addCleanup(context.stop)

    def test_omitting_registered_guest_refuses_any_host_write(self):
        with self.assertRaisesRegex(RuntimeError,'all_registered'):
            m.restore_host_watermark(self.managers[:1])
        self.assertEqual(self.value.read_text(),'2965\n')

    def test_partial_rollback_failure_keeps_host_value_and_guard_policy_disabled(self):
        first={'ok':True,'restored_sha256':m.STOCK_SHA256}
        with patch.object(m,'guard_stopped',side_effect=[first,RuntimeError('injected_restore_failure')]):
            with self.assertRaisesRegex(RuntimeError,'injected_restore_failure'):
                m.rollback_producers(self.managers)
        self.assertEqual(self.value.read_text(),'2965\n')
        self.assertIs(json.loads(m.CONFIG.read_text())['enabled'],False)

    def test_active_worker_refuses_disable_restore_and_host_write(self):
        active=types.SimpleNamespace(returncode=0,stdout='LoadState=loaded\nMainPID=123\nActiveState=active\n')
        with patch.object(m.subprocess,'run',return_value=active),patch.object(m,'guard_stopped')as restore:
            with self.assertRaisesRegex(RuntimeError,'paused_workers'):m.rollback_producers(self.managers)
            restore.assert_not_called()
        self.assertFalse(m.CONFIG.exists());self.assertEqual(self.value.read_text(),'2965\n')

    def test_all_originals_restored_before_explicit_host_value_changes(self):
        seen=[]
        def restore(manager,owned,**kwargs):
            self.assertEqual(self.value.read_text(),'2965\n')
            self.assertIs(json.loads(m.CONFIG.read_text())['enabled'],False)
            seen.append(manager.context.instance_name)
            return {'ok':True,'restored_sha256':m.STOCK_SHA256}
        with patch.object(m,'guard_stopped',side_effect=restore):
            result=m.rollback_producers(self.managers,host_value=25)
        self.assertTrue(result['ok']);self.assertEqual(seen,['phone-1','phone-2'])
        self.assertEqual(self.value.read_text(),'25\n')
        self.assertFalse(result['bad_captured_factor_restored_automatically'])


if __name__=='__main__':unittest.main()
