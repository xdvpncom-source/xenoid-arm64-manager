#!/usr/bin/env python3
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'native/xenoid-profile-runtime/cold_seed.c'
IDS = {'android_id':'0123456789abcdef','boot_id':'ead23f1f-3290-4fd1-b9b4-fb4e88942d7e',
       'serial':'0123456789ABCDEF','imei':'351804610123456','imeisv':'62'}
ATTRIBUTE='user.xenoid.test.selinux'

def set_label(path,value):
    if hasattr(os,'setxattr'):os.setxattr(path,ATTRIBUTE,value)
    else:subprocess.run(['xattr','-wx',ATTRIBUTE,value.hex(),str(path)],check=True,capture_output=True)

def get_label(path):
    if hasattr(os,'getxattr'):return os.getxattr(path,ATTRIBUTE)
    return bytes.fromhex(subprocess.check_output(['xattr','-px',ATTRIBUTE,str(path)],text=True))

def remove_label(path):
    if hasattr(os,'removexattr'):os.removexattr(path,ATTRIBUTE)
    else:subprocess.run(['xattr','-d',ATTRIBUTE,str(path)],check=True,capture_output=True)


class NativeColdSeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('C compiler unavailable')
        cls.temp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temp.name) / 'cold-seed'
        subprocess.run([compiler,'-std=c11','-Wall','-Wextra','-Werror',
            '-Wno-misleading-indentation','-DCOLD_SEED_TESTING',
            '-fsanitize=address,undefined','-O1','-g',str(SOURCE),'-o',str(cls.binary)],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name).resolve()
        self.template = json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        self.template_path = self.directory/'template.json'
        self.template_path.write_text(json.dumps(self.template))
        self.profile = deepcopy(self.template)
        self.profile['ids'] = deepcopy(IDS)
        self.profile['usb']['serial'] = IDS['serial']
        self.profile_dir = self.directory/'profile'
        self.profile_dir.mkdir()
        for directory in (self.directory,self.profile_dir):
            set_label(directory,b'u:object_r:shell_data_file:s0\0')
        for key,value in IDS.items():
            (self.profile_dir/key).write_text(value+'\n')
        self.write_profile()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_profile(self):
        (self.profile_dir/'effective.json').write_text(json.dumps(self.profile))

    def protected(self):
        return {name: (self.profile_dir/name).read_bytes() for name in [*IDS,'effective.json']}

    def run_seed(self):
        result = subprocess.run([str(self.binary),str(self.template_path),str(self.profile_dir)],capture_output=True,text=True)
        self.assertNotIn('AddressSanitizer',result.stderr)
        self.assertNotIn('runtime error:',result.stderr)
        return result

    def test_valid_replay_preserves_ids_and_json_exact_bytes(self):
        before=self.protected()
        (self.profile_dir/'memory_totalBytes').write_text('broken\n')
        result=self.run_seed()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.protected(),before)
        report=json.loads(result.stdout)
        self.assertEqual(report['sidecars'],94)
        self.assertFalse(report['unique_ids_written'])
        self.assertEqual((self.profile_dir/'memory_totalBytes').read_text(),'6442450944\n')
        self.assertEqual((self.profile_dir/'input_x_maximum').read_text(),'1079\n')
        self.assertEqual((self.profile_dir/'input_y_maximum').read_text(),'2399\n')
        self.assertEqual((self.profile_dir/'display_density_dpi').read_text(),'420\n')
        self.assertEqual((self.profile_dir/'battery_capacityMah').read_text(),'4410\n')
        self.assertEqual((self.profile_dir/'cpu_6_model').read_text(),'Cortex-X1\n')
        self.assertEqual((self.directory/'xenoid-qa-display-mode').read_text(),'1080 2400 429\n')
        self.assertEqual((self.directory/'xenoid-qa-refresh-rate').read_text(),'60\n')
        self.assertFalse(any(path.name.startswith('.cold-seed') for path in self.profile_dir.iterdir()))
        for path in (self.profile_dir/'battery_level',self.profile_dir/'cpu_count',self.directory/'xenoid-qa-display-mode'):
            self.assertEqual(get_label(path),b'u:object_r:shell_data_file:s0\0')

    def test_missing_or_conflicting_directory_label_refuses_before_writes(self):
        before=self.protected()
        for target in (self.directory,self.profile_dir):
            for value in (None,b'u:object_r:system_data_file:s0\0'):
                if value is None:remove_label(target)
                else:set_label(target,value)
                result=self.run_seed()
                self.assertNotEqual(result.returncode,0)
                self.assertIn('profile_directory_label_mismatch',result.stderr)
                self.assertEqual(self.protected(),before)
                self.assertFalse((self.profile_dir/'cpu_count').exists())
                set_label(target,b'u:object_r:shell_data_file:s0\0')

    def test_reordered_json_and_live_battery_telemetry(self):
        self.profile['battery']['level']=80
        self.profile['battery']['chargeCounterUah']=3528000
        (self.profile_dir/'effective.json').write_text(json.dumps(self.profile,sort_keys=True,indent=2))
        before=self.protected()
        result=self.run_seed()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.protected(),before)
        self.assertEqual((self.profile_dir/'battery_level').read_text(),'80\n')

    def test_wrong_model_refuses_before_any_sidecar_write(self):
        self.profile['build']['model']='Pixel 6 Pro';self.write_profile()
        before=self.protected();names={x.name for x in self.profile_dir.iterdir()}
        result=self.run_seed()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('stored_profile_metadata_mismatch',result.stderr)
        self.assertEqual(self.protected(),before)
        self.assertEqual({x.name for x in self.profile_dir.iterdir()},names)

    def test_missing_profile_or_new_userdata_requires_official_bootstrap(self):
        (self.profile_dir/'effective.json').unlink()
        before={k:(self.profile_dir/k).read_bytes() for k in IDS}
        result=self.run_seed();self.assertNotEqual(result.returncode,0)
        self.assertIn('official_profile_apply_required',result.stderr)
        self.assertEqual({k:(self.profile_dir/k).read_bytes() for k in IDS},before)
        shutil.rmtree(self.profile_dir)
        result=self.run_seed();self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.profile_dir.exists())

    def test_native_id_mismatch_and_sentinels_are_refused(self):
        for ids in [dict(IDS,serial='OTHER-DEVICE'),self.template['ids']]:
            self.profile['ids']=ids;self.write_profile()
            before=self.protected();result=self.run_seed()
            self.assertNotEqual(result.returncode,0)
            self.assertIn('native_ids_missing_or_mismatched',result.stderr)
            self.assertEqual(self.protected(),before)

    def test_inconsistent_geometry_cpu_memory_battery_refused(self):
        cases=[('input','x',{'minimum':0,'maximum':1439}),
               ('display','densityDpi',560),('memory','totalBytes',12884901888),
               ('cpu','implementer','0x99'),('battery','capacityMah',5003)]
        for group,key,value in cases:
            with self.subTest(group=group,key=key):
                saved=deepcopy(self.profile);self.profile[group][key]=value;self.write_profile()
                before=self.protected();result=self.run_seed()
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.protected(),before)
                self.profile=saved

    def test_symlink_or_hardlinked_sidecar_is_refused_without_following(self):
        before=self.protected();target=self.profile_dir/'memory_totalBytes'
        target.symlink_to(self.profile_dir/'serial')
        result=self.run_seed();self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.protected(),before)
        target.unlink();os.link(self.profile_dir/'serial',target)
        result=self.run_seed();self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.protected(),before)

    def test_json_symlink_malformed_duplicate_and_wrong_types_are_refused(self):
        original=(self.profile_dir/'effective.json').read_bytes()
        invalid=[original[:-1],b'{"build":{},"build":{}}',b'[]',original.replace(b'"level": 67',b'"level": null')]
        for raw in invalid:
            (self.profile_dir/'effective.json').write_bytes(raw)
            result=self.run_seed();self.assertNotEqual(result.returncode,0)
        (self.profile_dir/'effective.json').unlink()
        (self.profile_dir/'effective.json').symlink_to(self.template_path)
        result=self.run_seed();self.assertNotEqual(result.returncode,0)

    def test_deterministic_malformed_input_samples_do_not_crash(self):
        payload=json.dumps(self.profile).encode()
        rng=random.Random(721)
        for _ in range(25):
            raw=bytearray(payload)
            for _ in range(3):raw[rng.randrange(len(raw))]=rng.randrange(32,127)
            (self.profile_dir/'effective.json').write_bytes(raw)
            result=self.run_seed()
            self.assertIn(result.returncode,(0,1))

    def test_exponent_integer_sidecar_is_rejected_before_writes(self):
        self.profile['cpu']['cores'][0]['maximumFrequencyKhz']=1.8e6
        self.write_profile();before=self.protected()
        result=self.run_seed()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.protected(),before)
        self.assertFalse((self.profile_dir/'cpu_count').exists())

    def test_embedded_nul_in_id_sidecar_is_rejected(self):
        (self.profile_dir/'serial').write_bytes(IDS['serial'].encode()+b'\x00junk\n')
        before=self.protected();result=self.run_seed()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.protected(),before)

    def test_officially_preseeded_new_boot_id_is_preserved(self):
        # Existing native seed-boot-identity.sh updates only this sidecar.
        new_boot='f21bcbf4-a736-4a89-a003-59b240c71aa5'
        (self.profile_dir/'boot_id').write_text(new_boot+'\n')
        before=self.protected();result=self.run_seed()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.protected(),before)

    def test_symlink_ancestor_is_rejected(self):
        actual=self.profile_dir
        link=self.directory/'linked';link.symlink_to(actual, target_is_directory=True)
        self.profile_dir=link
        before=self.protected();result=self.run_seed()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.protected(),before)

    def test_fifo_identity_sidecar_does_not_block(self):
        (self.profile_dir/'serial').unlink()
        os.mkfifo(self.profile_dir/'serial')
        result=subprocess.run([str(self.binary),str(self.template_path),str(self.profile_dir)],
                              capture_output=True,text=True,timeout=3)
        self.assertNotEqual(result.returncode,0)


if __name__=='__main__':
    unittest.main()
