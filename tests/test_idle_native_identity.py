"""Portable workerless identity-observation regressions; no Android calls."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

with patch.object(sys, 'path', [str(QA), *sys.path]):
    host = load('idle_native_identity_host', QA / 'script_host.py')
identity = load('idle_native_identity_reader', QA / 'identity_reservations.py')
BUILD = {key: json.loads((QA / 'default-bluejay-policy.json').read_text())['build'][key]
         for key in ('model', 'device', 'fingerprint')}
VALUES = {'android_id':'0123456789abcdef','serial':'TESTSERIAL001','imei':'351804610000001',
          'imeisv':'01','boot_id':'12345678-1234-4234-8234-123456789abc',
          'bluetooth_address':'F4:F5:E8:01:02:03','hostname':'android-test',
          'device_name':'Pixel 6a TEST','statfs_fsid':'0000000000000001',
          'drm_device_unique_id':'1'*32,'imsi':'310260000000001',
          'iccid':'89012600000000000001','msisdn':'+15550000001'}

def binding():
    iid='12345678-1234-4234-8234-123456789abc'
    return {'device_id':1,'instance_id':iid,
            'resource_tag':hashlib.sha256(b'xenoid-instance/v1\0'+iid.encode()).hexdigest()[:12],
            'container_id':'a'*64}

class IdleNativeIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root/'state'; self.state.mkdir()
        (self.state/'operation.lock').touch(mode=0o600)
        process=self.root/'17'; process.mkdir()
        (process/'stat').write_text('17 (init) '+' '.join(['S']+['0']*18+['123']+['0']*5))
        (process/'cgroup').write_text('0::/system.slice/docker-'+binding()['container_id']+'.scope\n')
        self.row={'Id':binding()['container_id'],'State':{'Pid':17,'Running':True}}
        self.timeouts=[]
        lines=[VALUES[k] for k in ('android_id','serial','imei','imeisv','boot_id',
               'bluetooth_address','hostname','device_name','statfs_fsid','drm_device_unique_id')]
        lines += [BUILD[k] for k in ('model','device','fingerprint')]
        def docker(args, timeout):
            self.timeouts.append(timeout)
            return {'ok':True,'stdout':'\n'.join(lines)+'\n'}
        self.manager=types.SimpleNamespace(
            context=types.SimpleNamespace(state_root=self.state,instance_id=binding()['instance_id'],resource_tag=binding()['resource_tag']),
            cfg=types.SimpleNamespace(),_owned_container_record=lambda **kw:(self.row,None),docker_exec=docker)
        original_lstat=Path.lstat
        def lstat(path):
            info=original_lstat(path)
            return types.SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_nlink=info.st_nlink,st_dev=info.st_dev,st_ino=info.st_ino)
        self.patches=[patch.dict(sys.modules,{'identity_reservations':identity}),
            patch.object(identity,'PROC',self.root),
            patch.object(identity,'read_location',return_value={'profile':{'sim':{k:VALUES[k] for k in ('imsi','iccid','msisdn')}}}),
            patch.object(Path,'lstat',lstat)]
        for item in self.patches:item.start()
    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def observe(self):
        return host.native_identity_observation(binding(),request_id='b'*32,manager=self.manager)
    def test_workerless_read_is_signed_and_binds_existing_runtime(self):
        result=self.observe();self.assertEqual(result['status'],'observed')
        proof=result['observation'];self.assertEqual(proof['values'],VALUES)
        self.assertEqual(proof['runtime']['start_ticks'],123)
        self.assertEqual(proof['request_id'],'b'*32)
        self.assertEqual(identity.verified(proof,'proof_sha256'),proof)
        self.assertTrue(all(0<x<=8 for x in self.timeouts));self.assertFalse((self.root/'runs').exists())
    def test_busy_existing_lock_skips_without_values(self):
        with (self.state/'operation.lock').open() as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            result=self.observe()
        self.assertEqual(result['reason'],'native_busy');self.assertNotIn('observation',result)
    def test_pending_generation_skips_before_any_device_read(self):
        (self.state/'device-regenerate-v3.json').touch()
        result=self.observe();self.assertEqual(result['reason'],'native_pending');self.assertFalse(self.timeouts)
    def test_replaced_CID_is_not_observed(self):
        self.row['Id']='c'*64
        self.assertEqual(self.observe()['status'],'unavailable');self.assertFalse(self.timeouts)
    def test_replaced_lock_path_rejects_readback(self):
        original=identity.observe
        def replace(reader):
            value=original(reader)
            (self.state/'operation.lock').unlink();(self.state/'operation.lock').touch(mode=0o600)
            return value
        with patch.object(identity,'observe',side_effect=replace):result=self.observe()
        self.assertEqual(result['status'],'unavailable');self.assertNotIn('observation',result)
    def test_unverified_location_does_not_publish_partial_ids(self):
        with patch.object(identity,'read_location',side_effect=identity.ReservationPending('identity_location_epoch_unverified')):
            result=self.observe()
        self.assertEqual(result['status'],'unavailable');self.assertNotIn('observation',result)
    def test_read_deadline_does_not_publish_partial_ids(self):
        clock=[0.0];original=self.manager.docker_exec
        def slow(*args,**kwargs):
            value=original(*args,**kwargs);clock[0]+=9;return value
        self.manager.docker_exec=slow
        with patch.object(host.time,'monotonic',side_effect=lambda:clock[0]):result=self.observe()
        self.assertEqual(result['reason'],'TimeoutError');self.assertNotIn('observation',result)
    def test_legacy_poll_and_empty_extended_poll_do_not_touch_native(self):
        with patch.object(host,'snapshot',return_value={'legacy':True}),patch.object(host,'native_identity_observation',side_effect=AssertionError('unexpected native call')):
            self.assertEqual(host.poll_items([{'device_id':1,'run_id':'d'*32}]),{'items':[{'legacy':True}]})
            self.assertEqual(host.poll_items({'runs':[],'identity_targets':[]}),{'items':[],'identity_observations':[]})

if __name__=='__main__':unittest.main()
