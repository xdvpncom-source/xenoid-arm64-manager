#!/usr/bin/env python3
"""Runtime-free native profile build/first-publication contracts."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from xenoid.artifacts import ArtifactBuilder, CONSUMER_TARGETS, PROFILE_RUNTIME_TARGETS, TARGETS
from xenoid.cold_profile import PROFILE_ARTIFACT_OUTPUTS, augment_partition_lines, normalize_cold_profile, prepare_context
from xenoid.runtime_image import RuntimeImageBuilder, _CONTEXT_INPUT_PATHS, compute_input_record
spec=importlib.util.spec_from_file_location('seed_cold_profile',ROOT/'scripts/seed-cold-profile.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)


def profile():
    value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
    value['ids']={'android_id':'0123456789abcdef','boot_id':'11111111-2222-4333-8444-555555555555',
                  'serial':'TEST000000000001','imei':'123456789012345','imeisv':'01'}
    value['usb']['serial']=value['ids']['serial']
    return value

class Contracts(unittest.TestCase):
    def test_profile_producer_source_rotates_both_image_keys_only_when_selected(self):
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        extra=('profiles/runtime-v1/scripts/make-runtime-context.sh','profiles/runtime-v1/scripts/patch-runtime-props.py','src/xenoid/cold_profile.py','scripts/seed-cold-profile.py','scripts/seed-profile-boot-id.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for relative in (*_CONTEXT_INPUT_PATHS,*extra):
                target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/relative,target)
            (root/'runtime/redroid').mkdir(parents=True,exist_ok=True)
            owner=object.__new__(RuntimeImageBuilder);owner.project_root=root
            def keys(selected):
                owner._cold_profile=selected
                record=compute_input_record(base_image_id='sha256:'+'1'*64,
                    artifact_tuples=[{'target':'daemon','inputSha256':'2'*64,'toolSha256':'3'*64,'outputs':[{'path':'daemon/app.apk','mode':'0644','size':1,'sha256':'4'*64}]}],
                    context_inputs=owner._context_inputs(),google_inputs={},tool_inputs={},builder_inputs={},
                    daemon_seed_contract={'packageName':'dev.example.daemon','sharedUserId':None,'systemDestination':'/system/priv-app/XenoidDaemon/XenoidDaemon.apk','signingLineage':['5'*64],'privilegedPermissions':[],'apkSha256':'4'*64,'versionCode':1})
                return record['inputSha256'],record['bootInputSha256']
            default_before=keys(None);selected_before=keys(value)
            path=root/'src/xenoid/cold_profile.py';path.write_bytes(path.read_bytes()+b'\n# producer input sensitivity\n')
            self.assertEqual(keys(None),default_before)
            selected_after=keys(value)
            self.assertNotEqual(selected_after[0],selected_before[0])
            self.assertNotEqual(selected_after[1],selected_before[1])
    def test_unmounted_dlkm_aliases_are_baked_into_loaded_system_properties(self):
        spec=importlib.util.spec_from_file_location('profile_props',ROOT/'profiles/runtime-v1/scripts/patch-runtime-props.py')
        props=importlib.util.module_from_spec(spec);spec.loader.exec_module(props)
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        generated=props.generated_partition_lines(value['build'])
        augment_partition_lines(generated,value)
        for scope in ('system_dlkm','odm_dlkm'):
            for suffix,key in (('display.id','id'),('description','description'),('version.security_patch','security_patch')):
                line=f"ro.{scope}.build.{suffix}={value['build'][key]}"
                self.assertEqual(generated['system_build.prop'].count(line),1)
                self.assertIn(line,generated[scope+'_build.prop'])
        legacy=props.generated_partition_lines(value['build']);before=deepcopy(legacy)
        augment_partition_lines(legacy,None)
        self.assertEqual(legacy,before)
    def test_declared_artifacts_are_not_part_of_legacy_context(self):
        builder=ArtifactBuilder(ROOT)
        self.assertEqual(builder._expand_targets(PROFILE_RUNTIME_TARGETS),list(PROFILE_RUNTIME_TARGETS))
        self.assertTrue(set(PROFILE_RUNTIME_TARGETS).isdisjoint(CONSUMER_TARGETS['runtimeContext']))
        self.assertEqual({output.path for name in PROFILE_RUNTIME_TARGETS for output in TARGETS[name].outputs},set(PROFILE_ARTIFACT_OUTPUTS.values()))
        static=TARGETS['profileColdSeed'].outputs[0]
        self.assertEqual((static.elf_type,static.interpreter),('EXEC',False))
        for name in PROFILE_RUNTIME_TARGETS:
            self.assertTrue(all((ROOT/path).exists() for path in TARGETS[name].sources))

    def test_profile_manifest_requires_exact_staged_artifacts(self):
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            stage=Path(temp)/'stage';out=Path(temp)/'context';(out/'payload').mkdir(parents=True)
            (out/'payload/xenoid.rc').write_text('on post-fs-data\n    exec_start xenoid-overlay\n')
            (out/'Dockerfile').write_text('FROM base\nENTRYPOINT ["/xenoid-init","/data/xenoid-rootfs.img","/data/xenoid-data.img","/init","androidboot.hardware=raven","androidboot.hardware.sku=G8V0U"]\n')
            hashes={}
            for destination,relative in PROFILE_ARTIFACT_OUTPUTS.items():
                path=stage/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(destination.encode())
                hashes[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
            prepare_context(ROOT,out,value,stage,hashes)
            self.assertIn('"/system/bin/xenoid-cold-seed","--init"',(out/'Dockerfile').read_text())
            (stage/next(iter(hashes))).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'staged_artifact_changed'):
                prepare_context(ROOT,out,value,stage,hashes)

    def test_first_profile_uses_supplied_canonical_ids(self):
        value=profile();leaves=seed.profile_leaves(value)
        self.assertEqual(len(leaves),108)
        self.assertEqual(leaves['serial'],b'TEST000000000001\n')
        self.assertNotIn(b'REGENERATE',leaves['effective.json'])
        value['ids']['serial']='REGENERATE'
        with self.assertRaises(seed.SeedError):seed.profile_leaves(value)

    def test_selected_image_policy_is_required(self):
        leaves=seed.profile_leaves(profile())
        with self.assertRaises(seed.SeedError):seed.validate_policy(b'/data(/.*)? u:object_r:system_data_file:s0\n',leaves)
        seed.validate_policy(b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n',leaves)
        with self.assertRaises(seed.SeedError):seed.validate_policy(b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n/data/local/tmp/xenoid-profile foo\n',leaves)

    def test_unrelated_ltp_policy_does_not_cover_owned_paths(self):
        leaves=seed.profile_leaves(profile())
        policy=b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n/data/local/tmp/ltp(/.*)? u:object_r:nativetest_data_file:s0\n'
        self.assertTrue(seed.validate_policy(policy,leaves))
        for pattern in (b'/data/local/tmp/xenoid-profile/serial',b'/data/local/tmp/xenoid-.*(/.*)?',b'/data/local/tmp/xenoid-profile/effective[.]json'):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(seed.SeedError,'image_profile_label_policy_conflict'):
                    seed.validate_policy(policy+pattern+b' u:object_r:nativetest_data_file:s0\n',leaves)
        with self.assertRaisesRegex(seed.SeedError,'image_profile_label_policy_unsupported'):
            seed.validate_policy(policy+b'/data/local/tmp/[ u:object_r:shell_data_file:s0\n',leaves)

    def test_request_cannot_claim_an_unrelated_volume(self):
        instance='11111111-2222-4333-8444-555555555555'
        tag=hashlib.sha256(b'xenoid-instance/v1\0'+instance.encode()).hexdigest()[:12]
        request={'volume':'xenoid-data-'+tag,'dataUuid':instance,'rootfsUuid':instance,'profile':profile(),
                 'labels':{'dev.xenoid.owner':'xenoid','dev.xenoid.schema':'1','dev.xenoid.instance_id':instance,'dev.xenoid.instance_name':'example','dev.xenoid.resource_tag':tag}}
        seed.validate_request(request)
        request['labels']={}
        with self.assertRaises(seed.SeedError):seed.validate_request(request)

@unittest.skipUnless(shutil.which('debugfs') and shutil.which('mkfs.ext4'),'e2fsprogs required for real image test')
class ImageContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.base=Path(cls.tmp.name)/'base.img'
        with cls.base.open('wb') as f:f.truncate(32*1024*1024)
        subprocess.run(['mkfs.ext4','-q','-F',str(cls.base)],check=True)
        cls.policy=b'/data/local/tmp(/.*)? u:object_r:shell_data_file:s0\n'
        cls.target=profile()
        result=seed.seed_image(seed.Image(cls.base),cls.target,policy=cls.policy)
        assert result['profileSeeded']
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def setUp(self):
        self.tmpcase=tempfile.TemporaryDirectory();self.path=Path(self.tmpcase.name)/'test.img';shutil.copyfile(self.base,self.path);self.image=seed.Image(self.path)
    def tearDown(self):self.tmpcase.cleanup()
    def test_real_uid_mode_context_and_idempotent_seed(self):
        result=seed.seed_image(self.image,self.target,policy=self.policy)
        self.assertTrue(result['profileSeeded'])
        self.assertEqual(self.image.read('/local/tmp/xenoid-profile/serial'),b'TEST000000000001\n')
        self.assertTrue(self.image.metadata_matches('/local/tmp/xenoid-profile',0,0,0o700,seed.FILE_LABEL))
    def test_seed_runtime_second_create_is_read_only(self):
        self.image.command('mkdir /system')
        target=deepcopy(self.target);target['ids']['boot_id']='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        result=seed.seed_image(self.image,target,policy=self.policy)
        self.assertTrue(result['alreadyInitialized'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
    def test_initialized_safe_directory_modes_are_preserved_and_unsafe_rejected(self):
        self.image.command('mkdir /system')
        for mode,accepted in ((0o700,True),(0o755,True),(0o770,False),(0o777,False)):
            with self.subTest(mode=oct(mode)):
                self.image.metadata('/local/tmp/xenoid-profile',0,0,mode,seed.FILE_LABEL)
                before=hashlib.sha256(self.path.read_bytes()).hexdigest()
                if accepted:
                    self.assertTrue(seed.seed_image(self.image,self.target,policy=self.policy)['alreadyInitialized'])
                else:
                    with self.assertRaisesRegex(seed.SeedError,'profile_metadata_mismatch'):
                        seed.seed_image(self.image,self.target,policy=self.policy)
                self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

    def test_old_preinit_missing_nonunique_labels_are_repaired_without_data_changes(self):
        self.image.command('mkdir /system')
        protected={name:self.image.read('/local/tmp/xenoid-profile/'+name) for name in ('android_id','boot_id','serial','imei','imeisv','effective.json','cpu_count','battery_level')}
        for name in ('cpu_count','battery_level'):
            self.image.command('ea_rm /local/tmp/xenoid-profile/'+name+' security.selinux')
        result=seed.seed_image(self.image,self.target,policy=self.policy)
        self.assertTrue(result['alreadyInitialized'])
        self.assertEqual(result['labelsRepaired'],2)
        for name,data in protected.items():
            path='/local/tmp/xenoid-profile/'+name
            self.assertEqual(self.image.read(path),data)
            self.assertTrue(self.image.metadata_matches(path,0,0,0o644,seed.FILE_LABEL))
    def test_conflicting_label_or_id_never_repairs_other_missing_label(self):
        self.image.command('mkdir /system')
        self.image.command('ea_rm /local/tmp/xenoid-profile/cpu_count security.selinux')
        self.image.set_label('/local/tmp/xenoid-profile/battery_level','u:object_r:system_data_file:s0')
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(seed.SeedError):seed.seed_image(self.image,self.target,policy=self.policy)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.image.set_label('/local/tmp/xenoid-profile/battery_level',seed.FILE_LABEL)
        target=deepcopy(self.target);target['ids']['serial']='OTHER00000000001';target['usb']['serial']=target['ids']['serial']
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(seed.SeedError):seed.seed_image(self.image,target,policy=self.policy)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
    def test_populated_other_identity_never_changes(self):
        self.image.command('mkdir /system')
        target=deepcopy(self.target);target['ids']['serial']='OTHER00000000001';target['usb']['serial']=target['ids']['serial']
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(seed.SeedError):seed.seed_image(self.image,target,policy=self.policy)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

if __name__=='__main__':unittest.main()
