#!/usr/bin/env python3
"""Runtime-free native Bluetooth target ownership and cold replay contracts."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bt_seed',ROOT/'scripts/seed-cold-profile.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
ADDRESS='02:13:24:35:46:57'


def profile():
    value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_bytes())
    value['ids']={'android_id':'0123456789abcdef','boot_id':'11111111-2222-4333-8444-555555555555',
                  'serial':'TEST000000000001','imei':'123456789012345','imeisv':'01'}
    value['usb']['serial']=value['ids']['serial'];return value


class SeedContracts(unittest.TestCase):
    def test_initial_address_is_native_identity_derived_and_unicast(self):
        ids=profile()['ids'];before=deepcopy(ids)
        a=seed.initial_bluetooth_address(ids)
        self.assertEqual(a,seed.initial_bluetooth_address(ids));self.assertEqual(ids,before)
        self.assertRegex(a,r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')
        self.assertEqual(int(a[:2],16)&3,2)
        values={seed.initial_bluetooth_address(dict(ids,serial='TEST%012d'%i))for i in range(100)}
        self.assertEqual(len(values),100)

    def test_leaf_is_separate_from_effective_json(self):
        p=profile();leaves=seed.profile_leaves(p)
        self.assertEqual(leaves['bluetooth_address'],(seed.initial_bluetooth_address(p['ids'])+'\n').encode())
        self.assertEqual(json.loads(leaves['effective.json']),p)
        self.assertFalse(seed.valid_bluetooth_sidecar(b'bad\n'))
        self.assertFalse(seed.valid_bluetooth_sidecar(ADDRESS.encode()))

    def test_recorded_gen_target_is_never_rederived(self):
        p=profile();leaves=seed.profile_leaves(p);leaves['bluetooth_address']=(ADDRESS+'\n').encode()
        class Image:
            def exists(self,path):return path in seed.LABELS or path=='/system' or path.startswith('/local/tmp/xenoid-profile/')
            def names(self,path):return {'local','system'} if path=='/'else set()
            def read(self,path):return leaves[path.rsplit('/',1)[-1]]
            def info(self,path):return 'Type: directory\n' if path in seed.LABELS else 'Type: regular\nLinks: 1\n'
            def metadata_matches(self,path,uid,gid,mode,label):
                if path in seed.LABELS:return (uid,gid,mode,label)==seed.LABELS[path]
                return (uid,gid,mode,label)==(0,0,0o600 if path.endswith('/bluetooth_address')else 0o644,seed.FILE_LABEL)
            def write_absent(self,*_):raise AssertionError('must not write existing target')
            def set_label(self,*_):raise AssertionError('must not rewrite existing target')
        result=seed.seed_image(Image(),p,policy=b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n')
        self.assertTrue(result['alreadyInitialized']);self.assertEqual(leaves['bluetooth_address'],(ADDRESS+'\n').encode())
        leaves['bluetooth_address']=b'invalid\n'
        with self.assertRaisesRegex(seed.SeedError,'profile_seed_conflict'):
            seed.seed_image(Image(),p,policy=b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n')


@unittest.skipUnless(shutil.which('cc'),'C compiler required')
class NativeWriter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name).resolve();cls.directory=cls.root/'profile';cls.binary=cls.root/'writer'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
            '-DXENOID_PROFILE_TESTING','-DPROFILE_DIR="'+str(cls.directory)+'"',
            str(ROOT/'native/xenoid-profile/xenoid_profile.c'),'-o',str(cls.binary)],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def setUp(self):
        if self.directory.exists():shutil.rmtree(self.directory)
        self.directory.mkdir(mode=0o755);self.path=self.directory/'bluetooth_address'

    def run_writer(self,value=ADDRESS):
        r=subprocess.run([str(self.binary),'set-bluetooth-address',value],capture_output=True,text=True)
        self.assertNotIn('AddressSanitizer',r.stderr);self.assertNotIn('runtime error:',r.stderr);return r

    def test_atomic_owner_mode_and_idempotent_exact_target(self):
        for _ in range(2):
            r=self.run_writer();self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(self.path.read_bytes(),(ADDRESS+'\n').encode())
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),0o600)
            self.assertTrue(json.loads(r.stdout)['durable'])
        self.assertEqual([p.name for p in self.directory.iterdir()],['bluetooth_address'])

    def test_invalid_old_target_is_not_replaced(self):
        self.path.write_bytes(b'invalid\n');self.path.chmod(0o600)
        self.assertNotEqual(self.run_writer().returncode,0)
        self.assertEqual(self.path.read_bytes(),b'invalid\n')

    def test_symlink_and_hardlink_not_followed(self):
        outside=self.root/'outside';outside.write_text(ADDRESS+'\n');outside.chmod(0o600)
        self.path.symlink_to(outside);self.assertNotEqual(self.run_writer().returncode,0)
        self.path.unlink();os.link(outside,self.path);self.assertNotEqual(self.run_writer().returncode,0)
        self.assertEqual(outside.read_text(),ADDRESS+'\n')

    def test_bad_input_and_directory_mode_refused(self):
        self.assertNotEqual(self.run_writer('02:13:24:35:46:57;id').returncode,0);self.assertFalse(self.path.exists())
        self.directory.chmod(0o777);self.assertNotEqual(self.run_writer().returncode,0);self.assertFalse(self.path.exists())

    def test_private_and_readable_directories_both_supported(self):
        for mode in (0o700, 0o755):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                result=self.run_writer()
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode),mode)

    def test_group_write_world_write_and_special_modes_refused(self):
        for mode in (0o770, 0o777, 0o1755):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                self.assertNotEqual(self.run_writer().returncode,0)
                self.assertFalse(self.path.exists())

    def test_directory_symlink_is_not_followed(self):
        outside=self.root/'outside-dir';self.directory.rename(outside)
        try:
            self.directory.symlink_to(outside,target_is_directory=True)
            self.assertNotEqual(self.run_writer().returncode,0)
            self.assertFalse((outside/'bluetooth_address').exists())
        finally:
            self.directory.unlink();outside.rename(self.directory)

    def test_fsync_barriers_surround_atomic_publication(self):
        text=(ROOT/'native/xenoid-profile/xenoid_profile.c').read_text()
        self.assertLess(text.index('if(fsync(fd))'),text.index('if(renameat(directory'))
        self.assertLess(text.index('if(renameat(directory'),text.index('if(fsync(directory))'))


class BootReplay(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.profile=self.root/'profile';self.profile.mkdir()
        self.path=self.profile/'bluetooth_address';self.db=self.root/'props.json';self.bin=self.root/'bin';self.bin.mkdir()
        stub='''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['BT_DB']);v=json.loads(p.read_text())
if Path(sys.argv[0]).name=='getprop':print(v.get(sys.argv[1],''))
elif Path(sys.argv[0]).name=='setprop':
 v[sys.argv[1]]=sys.argv[2];v.setdefault('_calls',[]).append(sys.argv[1]);p.write_text(json.dumps(v))
else:
 t=Path(sys.argv[-1]);print(str(t.stat().st_size)if sys.argv[2]=='%s'else '0:0:'+oct(t.stat().st_mode&0o7777)[2:]if t.is_dir()else '0:0:'+oct(t.stat().st_mode&0o7777)[2:]+':'+str(t.stat().st_nlink))
'''
        for name in ('getprop','setprop','stat'):
            p=self.bin/name;p.write_text(stub);p.chmod(0o755)
        text=(ROOT/'runtime/redroid/xenoid-bluetooth-address.sh').read_text()
        self.assertEqual(text.count('profile=/data/local/tmp/xenoid-profile'),1)
        self.script=self.root/'replay.sh';self.script.write_text(text.replace('profile=/data/local/tmp/xenoid-profile','profile='+shlex.quote(str(self.profile))))

    def tearDown(self):self.tmp.cleanup()

    def run_replay(self,props):
        self.db.write_text(json.dumps(props));env=dict(os.environ,PATH=str(self.bin)+':'+os.environ['PATH'],BT_DB=str(self.db))
        r=subprocess.run(['sh',str(self.script)],env=env,capture_output=True,text=True)
        return r,json.loads(self.db.read_text())

    def test_sidecar_wins_and_no_file_is_generated(self):
        self.path.write_text(ADDRESS+'\n');self.path.chmod(0o600)
        before=self.path.read_bytes()
        r,v=self.run_replay({'ro.persistent_properties.ready':'true','persist.vendor.bt.rootcanal_mac_address':'A4:A5:A6:A7:A8:A9'})
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(v['vendor.bt.rootcanal_mac_address'],ADDRESS);self.assertEqual(v['persist.vendor.bt.rootcanal_mac_address'],ADDRESS)
        self.assertEqual(self.path.read_bytes(),before)

    def test_exact_safe_directory_modes_and_unsafe_modes(self):
        self.path.write_text(ADDRESS+'\n');self.path.chmod(0o600)
        for mode,accepted in ((0o700,True),(0o755,True),(0o770,False),(0o777,False),(0o1755,False)):
            with self.subTest(mode=oct(mode)):
                self.profile.chmod(mode)
                r,v=self.run_replay({'ro.persistent_properties.ready':'true'})
                self.assertEqual(r.returncode==0,accepted,r.stderr)
                if not accepted:self.assertEqual(v['_calls'],['vendor.bt.xenoid_address_ready'])

    def test_directory_symlink_is_rejected(self):
        self.path.write_text(ADDRESS+'\n');self.path.chmod(0o600)
        outside=self.root/'outside-dir';self.profile.rename(outside)
        try:
            self.profile.symlink_to(outside,target_is_directory=True)
            r,v=self.run_replay({'ro.persistent_properties.ready':'true'})
            self.assertNotEqual(r.returncode,0);self.assertEqual(v['_calls'],['vendor.bt.xenoid_address_ready'])
        finally:
            self.profile.unlink();outside.rename(self.profile)

    def test_valid_persisted_target_is_replayed_without_sidecar_generation(self):
        r,v=self.run_replay({'ro.persistent_properties.ready':'true','persist.vendor.bt.rootcanal_mac_address':ADDRESS})
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(v['vendor.bt.rootcanal_mac_address'],ADDRESS)
        self.assertFalse(self.path.exists())

    def test_missing_or_invalid_existing_target_fails_closed(self):
        r,v=self.run_replay({'ro.persistent_properties.ready':'true'});self.assertNotEqual(r.returncode,0);self.assertEqual(v.get('_calls'),['vendor.bt.xenoid_address_ready'])
        self.path.write_text('bad\n');self.path.chmod(0o600)
        r,v=self.run_replay({'ro.persistent_properties.ready':'true','persist.vendor.bt.rootcanal_mac_address':ADDRESS})
        self.assertNotEqual(r.returncode,0);self.assertEqual(v.get('_calls'),['vendor.bt.xenoid_address_ready']);self.assertEqual(self.path.read_text(),'bad\n')

    def test_before_persist_loader_only_ephemeral_property_is_written(self):
        self.path.write_text(ADDRESS+'\n');self.path.chmod(0o600)
        r,v=self.run_replay({'ro.persistent_properties.ready':'false'})
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(v['_calls'],['vendor.bt.xenoid_address_ready','vendor.bt.rootcanal_mac_address','vendor.bt.xenoid_address_ready'])


@unittest.skipUnless(shutil.which('javac')and shutil.which('java'),'JDK required')
class JavaPublisher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        harness=cls.root/'BluetoothMain.java';harness.write_text('''package dev.xenoid.daemon;
public class BluetoothMain { public static void main(String[] a) {
try { System.out.print(BluetoothIdentity.publishCommand(a[0])); }
catch(IllegalArgumentException e){System.exit(2);} }}''')
        subprocess.run(['javac','-d',str(cls.root),str(ROOT/'daemon/app/src/main/java/dev/xenoid/daemon/BluetoothIdentity.java'),str(harness)],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def invoke(self,value):return subprocess.run(['java','-cp',str(self.root),'dev.xenoid.daemon.BluetoothMain',value],capture_output=True,text=True)

    def test_durable_native_writer_precedes_controller_properties_and_settings(self):
        r=self.invoke(' 02:13:24:35:46:57 ');self.assertEqual(r.returncode,0)
        commands=r.stdout
        self.assertLess(commands.index('set-bluetooth-address'),commands.index('setprop persist.vendor'))
        self.assertLess(commands.index('setprop persist.vendor'),commands.index('setprop vendor.bt.rootcanal_mac_address'))
        self.assertLess(commands.index('setprop vendor.bt.rootcanal_mac_address'),commands.index('settings --user'))
        self.assertNotIn('sync ',commands)
        subprocess.run(['sh','-n'],input=commands,text=True,check=True,capture_output=True)

    def test_invalid_input_never_becomes_shell(self):
        for text in ('bad',ADDRESS+';id',ADDRESS+'\nBAD'):
            r=self.invoke(text);self.assertEqual(r.returncode,2);self.assertEqual(r.stdout,'')


if __name__=='__main__':unittest.main()
