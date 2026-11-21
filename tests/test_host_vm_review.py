"""Independent fault checks: guest mount cleanup must never walk the filesystem."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'qa/qa_host_memory.py'
spec = importlib.util.spec_from_file_location('host_vm_independent_review', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
UUID = '182267b0-a38b-427d-bff2-59c5d087661f'
CID = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64


class OfflineMountFailureReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / 'xenoid-rootfs.img'
        self.image.write_bytes(b'untouched-rootfs-fixture')
        info = self.image.lstat()
        self.payload = {'image': str(self.image), 'device': info.st_dev, 'inode': info.st_ino,
                        'size': info.st_size, 'instance_id': UUID, 'rootfs_uuid': UUID,
                        'container_id': CID, 'image_id': IMAGE, 'docker_base': ['docker'],
                        'owner_labels': {'dev.xenoid.owner': 'xenoid'}}
        self.mountpoint = self.root / 'private-mount'
        self.mountpoint.mkdir()
        self.sentinel = self.mountpoint / 'DO-NOT-DELETE-GUEST-DATA'
        self.detached = self.root / 'detached-guest-data'
        self.mounted = False
        self.mount_timeout = False
        self.umount_failure = False
        self.commands = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        original_lstat = Path.lstat
        def root_owned(path, *args, **kwargs):
            original = original_lstat(path, *args, **kwargs)
            if path == self.image:
                return SimpleNamespace(st_uid=0, st_gid=0, st_nlink=1, st_mode=original.st_mode,
                                       st_dev=original.st_dev, st_ino=original.st_ino, st_size=original.st_size)
            return original
        self.stack.enter_context(patch.object(Path, 'lstat', root_owned))
        self.stack.enter_context(patch.object(m, 'STATE', self.root / 'state'))
        self.uuid = self.stack.enter_context(patch.object(m, 'image_uuid', return_value=UUID))
        self.loops = self.stack.enter_context(patch.object(m, 'active_image_loops', return_value=[]))
        self.stopped = self.stack.enter_context(patch.object(m, 'stopped_container_probe'))
        self.producer = self.stack.enter_context(patch.object(m, 'patch_producer', return_value={'ok': True}))
        self.mkdir = self.stack.enter_context(patch.object(m.tempfile, 'mkdtemp', return_value=str(self.mountpoint)))
        self.stack.enter_context(patch.object(m, 'mountpoint_present', side_effect=lambda _name: self.mounted))
        self.run = self.stack.enter_context(patch.object(m.subprocess, 'run', side_effect=self.command))
        self.rmdir = self.stack.enter_context(patch.object(m.os, 'rmdir', wraps=os.rmdir))
        self.rmtree = self.stack.enter_context(patch.object(shutil, 'rmtree', wraps=shutil.rmtree))

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == 'mount':
            self.mounted = True
            self.sentinel.write_text('preserve guest contents')
            if self.mount_timeout:
                raise subprocess.TimeoutExpired(args, kwargs.get('timeout', 12))
        elif args[0] == 'umount':
            if self.umount_failure:
                raise subprocess.CalledProcessError(1, args)
            self.sentinel.rename(self.detached)
            self.mounted = False
        else:
            raise AssertionError('Unexpected command in offline mount fixture')
        return subprocess.CompletedProcess(args, 0, b'', b'')

    def test_umount_failure_keeps_guest_sentinel_and_never_recursively_cleans(self):
        self.umount_failure = True
        with self.assertRaises(subprocess.CalledProcessError):
            m.offline_child(self.payload)
        self.assertTrue(self.sentinel.is_file())
        self.assertEqual(self.sentinel.read_text(), 'preserve guest contents')
        self.assertTrue(self.mounted)
        self.rmtree.assert_not_called()
        self.rmdir.assert_not_called()

    def test_mount_timeout_with_ambiguous_success_and_failed_umount_keeps_guest(self):
        self.mount_timeout = True
        self.umount_failure = True
        with self.assertRaises((subprocess.TimeoutExpired, subprocess.CalledProcessError)):
            m.offline_child(self.payload)
        self.assertTrue(self.sentinel.is_file())
        self.producer.assert_not_called()
        self.assertEqual([c[0] for c in self.commands], ['mount', 'umount'])
        self.rmtree.assert_not_called()
        self.rmdir.assert_not_called()

    def test_mount_timeout_attempts_safe_unmount_but_never_patches_guest(self):
        self.mount_timeout = True
        with self.assertRaises(subprocess.TimeoutExpired):
            m.offline_child(self.payload)
        self.assertTrue(self.detached.is_file())
        self.assertFalse(self.mountpoint.exists())
        self.producer.assert_not_called()
        self.rmtree.assert_not_called()
        self.rmdir.assert_called_once_with(str(self.mountpoint))

    def test_success_unmounts_before_nonrecursive_removal(self):
        result = m.offline_child(self.payload)
        self.assertTrue(result['ok'])
        self.assertEqual([c[0] for c in self.commands], ['mount', 'umount'])
        self.assertFalse(self.mounted)
        self.assertTrue(self.detached.is_file())
        self.rmtree.assert_not_called()
        self.rmdir.assert_called_once_with(str(self.mountpoint))

    def test_wrong_uuid_never_mounts_or_patches(self):
        self.uuid.return_value = 'wrong-uuid'
        with self.assertRaisesRegex(RuntimeError, 'offline_image_changed'):
            m.offline_child(self.payload)
        self.assertEqual(self.commands, [])
        self.producer.assert_not_called()
        self.mkdir.assert_not_called()
        self.assertEqual(self.image.read_bytes(), b'untouched-rootfs-fixture')

    def test_guest_started_after_mount_is_rejected_before_producer_change(self):
        self.stopped.side_effect = [None, RuntimeError('host_vm_offline_guest_changed')]
        with self.assertRaisesRegex(RuntimeError, 'offline_guest_changed'):
            m.offline_child(self.payload)
        self.producer.assert_not_called()
        self.assertEqual([c[0] for c in self.commands], ['mount', 'umount'])
        self.assertTrue(self.detached.is_file())
        self.rmtree.assert_not_called()

    def test_uuid_change_after_mount_is_rejected_before_producer_change(self):
        self.uuid.side_effect = [UUID, 'wrong-uuid']
        with self.assertRaisesRegex(RuntimeError, 'offline_uuid_changed'):
            m.offline_child(self.payload)
        self.producer.assert_not_called()
        self.assertEqual([c[0] for c in self.commands], ['mount', 'umount'])
        self.assertTrue(self.detached.is_file())
        self.rmtree.assert_not_called()


class LoopAndDockerFenceReview(unittest.TestCase):
    def test_renamed_deleted_loop_backing_inode_is_still_active(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / 'xenoid-rootfs.img'
            image.write_bytes(b'fixture')
            info = image.stat()
            row = {'name': '/dev/loop7', 'back-file': '/different/path/renamed.img (deleted)',
                   'back-ino': info.st_ino, 'back-maj:min': f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'}
            answer = subprocess.CompletedProcess([], 0, json.dumps({'loopdevices': [row]}), '')
            with patch.object(m.subprocess, 'run', return_value=answer):
                self.assertEqual(m.active_image_loops(image), ['/dev/loop7'])
            row['back-ino'] += 1
            answer.stdout = json.dumps({'loopdevices': [row]})
            with patch.object(m.subprocess, 'run', return_value=answer):
                self.assertEqual(m.active_image_loops(image), [])

    def test_failed_loop_inventory_is_not_misreported_as_no_writers(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / 'xenoid-rootfs.img'
            image.write_bytes(b'fixture')
            with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
                with self.assertRaisesRegex(RuntimeError, 'loop_inventory_unavailable'):
                    m.active_image_loops(image)

    def test_child_rejects_running_pid_or_wrong_owned_image_container_labels(self):
        payload = {'docker_base': ['docker'], 'container_id': CID, 'image_id': IMAGE,
                   'owner_labels': {'dev.xenoid.owner': 'xenoid'}}
        base = {'Id': CID, 'Image': IMAGE, 'State': {'Running': False, 'Pid': 0},
                'Config': {'Labels': {'dev.xenoid.owner': 'xenoid'}}}
        good = subprocess.CompletedProcess([], 0, json.dumps([base]), '')
        with patch.object(m.subprocess, 'run', return_value=good):
            m.stopped_container_probe(payload)
        cases = [dict(base, Id='c' * 64), dict(base, Image='sha256:' + 'd' * 64),
                 dict(base, State={'Running': True, 'Pid': 100}),
                 dict(base, State={'Running': False, 'Pid': 100}),
                 dict(base, Config={'Labels': {'dev.xenoid.owner': 'foreign'}})]
        for changed in cases:
            with self.subTest(container=changed):
                response = subprocess.CompletedProcess([], 0, json.dumps([changed]), '')
                with patch.object(m.subprocess, 'run', return_value=response):
                    with self.assertRaisesRegex(RuntimeError, 'offline_guest_changed'):
                        m.stopped_container_probe(payload)


if __name__ == '__main__':
    unittest.main()


class RollbackLockHoldReview(unittest.TestCase):
    def fixture(self,stack,root):
        held=set();restored=set();events=[];inside={'host':False};expected={'phone-1','phone-2'}
        @contextlib.contextmanager
        def native_lock(name):
            self.assertFalse(inside['host'],'Never wait for an instance lock while holding host lock')
            held.add(name);events.append(('native-enter',name))
            try:yield
            finally:held.remove(name);events.append(('native-exit',name))
        @contextlib.contextmanager
        def host_lock():
            self.assertEqual(held,expected,'Every affected instance must remain locked')
            inside['host']=True
            try:yield
            finally:inside['host']=False
        class Knob:
            value='2965\n'
            def read_text(inner):return inner.value
            def write_text(inner,value):
                self.assertEqual(held,expected)
                self.assertTrue(inside['host'])
                self.assertEqual(restored,expected,'No host write until every original is restored')
                events.append(('host-write',value));inner.value=value
        knob=Knob()
        managers=[]
        for n in [2,1]:
            name='phone-'+str(n);owned={'Id':str(n)*64,'State':{'Running':False,'Pid':0}}
            managers.append(SimpleNamespace(context=SimpleNamespace(instance_name=name),
                _owned_container_record=lambda row=owned,**_: (row,None),
                instance_operation_lock=lambda selected=name,**_:native_lock(selected)))
        def restore(manager,owned,**kwargs):
            self.assertEqual(held,expected);self.assertTrue(inside['host'])
            self.assertIs(json.loads((root/'config.json').read_text())['enabled'],False)
            self.assertIs(kwargs['restore'],True)
            restored.add(manager.context.instance_name)
            return {'ok':True,'restored_sha256':m.STOCK_SHA256}
        for key,value in [('CONFIG',root/'config.json'),('STATE',root/'state'),('WATERMARK',knob)]:
            stack.enter_context(patch.object(m,key,value))
        stack.enter_context(patch.object(m.os,'geteuid',return_value=0))
        stack.enter_context(patch.object(m,'require_registered_managers'))
        stack.enter_context(patch.object(m,'host_lock',host_lock))
        stack.enter_context(patch.object(m,'configured_watermark',return_value=(25,'persisted administrator fixture')))
        stack.enter_context(patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'LoadState=loaded\nMainPID=0\nActiveState=inactive\n','')))
        mock=stack.enter_context(patch.object(m,'guard_stopped',side_effect=restore))
        return managers,held,restored,events,knob,mock,restore

    def test_all_native_locks_remain_held_through_disable_restore_and_host_write(self):
        with tempfile.TemporaryDirectory()as directory,contextlib.ExitStack()as stack:
            managers,held,restored,events,knob,_,_=self.fixture(stack,Path(directory))
            answer=m.rollback_producers(managers)
            self.assertTrue(answer['ok']);self.assertEqual(knob.value,'25\n')
            self.assertEqual(restored,{'phone-1','phone-2'});self.assertEqual(held,set())
            self.assertEqual(events[:2],[('native-enter','phone-1'),('native-enter','phone-2')])
            first_release=next(i for i,event in enumerate(events)if event[0]=='native-exit')
            write=next(i for i,event in enumerate(events)if event[0]=='host-write')
            self.assertLess(write,first_release)

    def test_failed_restore_releases_all_locks_without_writing_host_value(self):
        with tempfile.TemporaryDirectory()as directory,contextlib.ExitStack()as stack:
            managers,held,restored,events,knob,mock,restore=self.fixture(stack,Path(directory))
            def fail_second(manager,owned,**kwargs):
                if manager.context.instance_name=='phone-2':raise RuntimeError('injected-restore-failure')
                return restore(manager,owned,**kwargs)
            mock.side_effect=fail_second
            with self.assertRaisesRegex(RuntimeError,'injected-restore-failure'):m.rollback_producers(managers)
            self.assertEqual(knob.value,'2965\n');self.assertEqual(held,set())
            self.assertFalse(any(event[0]=='host-write'for event in events))
            self.assertIs(json.loads((Path(directory)/'config.json').read_text())['enabled'],False)
