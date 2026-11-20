"""Actual Native18 authentication and provisioning, independent of Android/ADB."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import types
import unittest
from collections.abc import Mapping
from typing import Any,Optional

from test_proxy_observer_classification import patched_observer
SOURCE='shipped-native-backend-patch'
PATCHED_SOURCE=patched_observer()
NAMES={'_rootd_authentication','_rootd_owned_authentication','_rootd_deployed_candidate','ensure_rootd_root'}
REVIEWED_BACKEND_SHA256='c42b8f7cc122e6c597a44fd0cd1969d5e2b5f728e0366f7f0aa80387dd5b0ebc'
tree=ast.parse(PATCHED_SOURCE);methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in NAMES]
assert len(methods)==len(NAMES)
assert hashlib.sha256('\n'.join(ast.get_source_segment(PATCHED_SOURCE,n) for n in sorted(methods,key=lambda n:n.name)).encode()).hexdigest()=='f3d64a3466972e69f3c828483e3c8b0309568f2f8e9bfb0770533b64b037bfbc'
class Clock:
 value=0.0
 @classmethod
 def now(cls):return cls.value
 @classmethod
 def sleep(cls,n):cls.value+=n
namespace={'Optional':Optional,'Any':Any,'Mapping':Mapping,'Path':Path,'hashlib':hashlib,'stat':stat,'re':re,'time':types.SimpleNamespace(monotonic=Clock.now,sleep=Clock.sleep),'subprocess':subprocess,'which':lambda name:'/usr/bin/'+name,'InstanceError':RuntimeError,'DaemonClient':object,'_ROOTD_PROVISION_TIMEOUT_SECONDS':30,'_ROOTD_REMOTE_PATH':'/data/local/tmp/xenoid-rootd','_ROOTD_LEGACY_PATH':'/data/local/tmp/.netd-helper','_ROOTD_RUN_DIRECTORY':'/data/local/tmp/xenoid-rootd-run','_ROOTD_PROCESS_RECORD':'/data/local/tmp/xenoid-rootd-run/process.json'}
owner=ast.ClassDef(name='Native',bases=[],keywords=[],body=methods,decorator_list=[])
exec(compile(ast.fix_missing_locations(ast.Module(body=[owner],type_ignores=[])),str(SOURCE),'exec'),namespace)
Native=namespace['Native'];CID='a'*64;TOKEN='b'*32;PAYLOAD=b'published-rootd';DIGEST=hashlib.sha256(PAYLOAD).hexdigest();REMOTE=namespace['_ROOTD_REMOTE_PATH']
class Client:
 def __init__(self,fixture):self.fixture=fixture;self.status=None;self.calls=[];self.raises=None
 def root_status(self,**kwargs):
  self.calls.append(kwargs)
  if self.raises:raise self.raises
  return self.status
 def read_private_token(self,**kwargs):return TOKEN
class Fixture(Native):
 def __init__(self,path):
  self.path=path;self.path.write_bytes(PAYLOAD);self.cid=CID;self.alive=True;self.state='valid';self.lease=types.SimpleNamespace(rootd_port=18767)
  self.process={'pid':4182,'startTime':4740166,'digest':DIGEST,'executable':REMOTE}
  self.metadata=(1795,3,0,stat.S_IFREG|0o755,1,len(PAYLOAD));self.deployed_digest=DIGEST
  self.client=Client(self);self.probes=[];self.kills=[];self.pushes=[];self.launches=[];self.writes=[];self.container_reads=[];self.record_reads=0
  self.response={'ok':True,'exitCode':0,'stdout':'0\n'};self.probe_raises=None;self.after_probe=None;self.after_owned=None;self.after_digest=None;self.after_cleanup=None
  self.listener_conflict=False;self.port_reads=0;self.port_hook=None;self.push_result=True;self.forbid_push=False
 def _owned_container_record(self,timeout):self.container_reads.append(timeout);return {'Id':self.cid},None
 def _rootd_owned_process(self,**kwargs):
  self.record_reads+=1;value=(dict(self.process),'valid') if self.alive and self.state=='valid' else (None,self.state if self.alive else 'absent')
  if self.after_owned:self.after_owned(self)
  return value
 def _rootd_exec(self,command,token,**kwargs):
  self.probes.append((command,token,kwargs))
  if self.after_probe:self.after_probe(self)
  if self.probe_raises:raise self.probe_raises
  return self.response
 def _rootd_stat(self,path,**kwargs):assert path==REMOTE;return self.metadata
 def _rootd_remote_digest(self,path,**kwargs):
  assert path==REMOTE
  result=self.deployed_digest
  if self.after_digest:self.after_digest(self)
  return result
 def _rootd_port_inodes(self,**kwargs):
  self.port_reads+=1
  if self.port_hook:self.port_hook(self)
  return ({'448399'} if self.alive else set()),self.listener_conflict
 def _rootd_output(self,args,**kwargs):assert args==['getprop','ro.product.cpu.abi'];return 'arm64-v8a'
 def _artifact_output(self,*args):return self.path
 def migrate_legacy_token_state(self):pass
 def _remove_legacy_android_rootd_token(self,*args,**kwargs):return True
 def daemon_client(self,**kwargs):return self.client
 def _terminate_rootd_process(self,identity,**kwargs):self.kills.append(dict(identity));self.alive=False;return True
 def _recover_current_rootd_orphan(self,*args,**kwargs):raise AssertionError('not an orphan')
 def _adopt_legacy_rootd(self,*args,**kwargs):raise AssertionError('no legacy adoption')
 def adb(self,args,**kwargs):
  assert not self.forbid_push,'ADB must not be required for identical deployed helper'
  self.pushes.append(args)
  if self.push_result:self.metadata=(1795,4,0,stat.S_IFREG|0o755,1,len(PAYLOAD));self.deployed_digest=DIGEST
  return {'ok':self.push_result}
 def docker_exec(self,args,**kwargs):
  self.writes.append(args)
  if args==['rm','-f','/data/local/tmp/.netd-helper'] and self.after_cleanup:self.after_cleanup(self)
  return {'ok':True}
 def _launch_rootd_with_token(self,cid,token,timeout):
  assert cid==CID and token==TOKEN
  self.launches.append((cid,timeout));self.alive=True;self.state='valid';self.process={'pid':5000,'startTime':4740200,'digest':DIGEST,'executable':REMOTE};return True
class Tests(unittest.TestCase):
 def setUp(self):
  Clock.value=0;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.f=Fixture(Path(self.tmp.name)/'rootd')
 def auth(self,f=None,**kw):
  f=f or self.f;return f._rootd_owned_authentication(f.client,dict(f.process),CID,DIGEST,TOKEN,deadline=kw.get('deadline',10))
 def test_primary_authenticated_daemon_remains_first_and_needs_no_direct_probe(self):
  self.f.client.status={'ok':True,'uid':0};self.assertEqual(self.auth(),'ok');self.assertFalse(self.f.probes)
 def test_unavailable_daemon_reuses_current_owned_rootd_without_kill_push_or_launch(self):
  f=self.f;r=f.ensure_rootd_root(timeout=2);self.assertTrue(r['ok']);self.assertTrue(r['reused']);self.assertEqual(len(f.probes),1);self.assertFalse(f.kills or f.pushes or f.launches)
 def test_primary_unauthorized_never_uses_direct_fallback_or_kills(self):
  for status in ({'ok':False,'httpStatus':401},{'ok':True,'root':True,'httpStatus':401},{'error':'daemon_unauthorized'},{'errorCode':'rootd_unauthorized'}):
   with self.subTest(status=status):
    self.f.client.status=status;self.assertEqual(self.f.ensure_rootd_root(timeout=2)['code'],'rootd_unauthorized');self.assertFalse(self.f.probes or self.f.kills or self.f.pushes)
 def test_dead_daemon_client_uses_owned_current_protocol_only(self):
  self.f.client.raises=OSError('offline');self.assertEqual(self.auth(),'ok');self.assertEqual(self.f.probes[0][0:2],('id -u',TOKEN))
 def test_bad_direct_result_never_reuses_or_kills_owned_current_helper(self):
  for response in (None,{}, {'ok':True,'exitCode':1,'stdout':'0'},{'ok':True,'exitCode':False,'stdout':'0'},{'ok':True,'exitCode':0,'stdout':'2000'},{'ok':True,'exitCode':0,'stdout':0},{'ok':False,'error':'unauthorized'}):
   with self.subTest(response=response):
    self.f.response=response;r=self.f.ensure_rootd_root(timeout=2);self.assertFalse(r['ok']);self.assertFalse(self.f.kills or self.f.pushes or self.f.launches)
 def test_direct_transport_failure_keeps_owned_current_helper(self):
  self.f.probe_raises=TimeoutError();self.assertEqual(self.auth(),'rootd_unavailable');self.assertFalse(self.f.kills)
 def test_nonowned_digest_path_pid_and_token_never_get_direct_probe(self):
  f=self.f
  for changes in ({'digest':'c'*64},{'executable':'/other'},{'pid':True},{'pid':1},{'startTime':False}):
   with self.subTest(changes=changes):
    owned={**f.process,**changes};self.assertEqual(f._rootd_owned_authentication(f.client,owned,CID,DIGEST,TOKEN,deadline=10),'rootd_resource_conflict')
  self.assertEqual(f._rootd_owned_authentication(f.client,f.process,CID,DIGEST,'invalid',deadline=10),'rootd_resource_conflict');self.assertFalse(f.probes)
 def test_invalid_process_record_is_not_a_fallback_candidate(self):
  self.f.state='invalid';self.assertEqual(self.f.ensure_rootd_root(timeout=2)['code'],'rootd_resource_conflict');self.assertFalse(self.f.probes or self.f.kills)
 def test_pid_start_digest_or_container_changed_during_probe_rejects(self):
  for key,value in (('pid',9999),('startTime',9999),('digest','c'*64),('executable','/other'),('cid','c'*64)):
   with self.subTest(key=key):
    f=Fixture(Path(self.tmp.name)/('rootd-'+key));f.after_probe=lambda obj,k=key,v=value:setattr(obj,'cid',v) if k=='cid' else obj.process.__setitem__(k,v)
    self.assertEqual(self.auth(f),'rootd_resource_conflict');self.assertFalse(f.kills)
 def test_container_change_while_reading_process_rejects_before_probe(self):
  self.f.after_owned=lambda f:setattr(f,'cid','c'*64);self.assertEqual(self.auth(),'rootd_resource_conflict');self.assertFalse(self.f.probes)
 def test_expired_deadline_does_not_probe_or_kill(self):
  self.assertEqual(self.auth(deadline=0),'rootd_unavailable');self.assertFalse(self.f.probes or self.f.kills)
 def test_budget_expired_by_direct_probe_is_not_success(self):
  self.f.after_probe=lambda f:setattr(Clock,'value',20);self.assertEqual(self.auth(),'rootd_unavailable');self.assertFalse(self.f.kills)
 def test_fresh_provision_uses_trusted_existing_file_without_any_adb(self):
  f=self.f;f.alive=False;f.forbid_push=True;r=f.ensure_rootd_root(timeout=2);self.assertTrue(r['ok']);self.assertFalse(r['reused']);self.assertEqual(len(f.launches),1);self.assertEqual(len(f.probes),1);self.assertFalse(f.pushes or f.kills)
 def test_fresh_provision_direct_auth_works_when_daemon_client_dead(self):
  f=self.f;f.alive=False;f.client.raises=ConnectionError('framework unavailable');f.forbid_push=True;self.assertTrue(f.ensure_rootd_root(timeout=2)['ok'])
 def test_untrusted_or_missing_deployed_file_uses_standard_adb_push(self):
  base=self.f.metadata
  for label,metadata,digest in [('missing',None,DIGEST),('uid',(*base[:2],2000,*base[3:]),DIGEST),('mode',(*base[:3],stat.S_IFREG|0o777,*base[4:]),DIGEST),('symlink',(*base[:3],stat.S_IFLNK|0o755,*base[4:]),DIGEST),('links',(*base[:4],2,base[5]),DIGEST),('size',(*base[:5],base[5]+1),DIGEST),('digest',base,'c'*64),('unreadable_digest',base,None)]:
   with self.subTest(label=label):
    f=Fixture(Path(self.tmp.name)/label);f.alive=False;f.metadata=metadata;f.deployed_digest=digest;r=f.ensure_rootd_root(timeout=2);self.assertTrue(r['ok']);self.assertEqual(len(f.pushes),1)
 def test_failed_standard_push_remains_failure_not_alternate_deployment(self):
  f=self.f;f.alive=False;f.metadata=None;f.push_result=False;self.assertEqual(f.ensure_rootd_root(timeout=2)['code'],'rootd_deploy_failed');self.assertEqual(len(f.pushes),1);self.assertFalse(f.launches)
 def test_deployed_file_or_container_race_before_launch_fails_without_overwrite(self):
  for change in ('file','container'):
   with self.subTest(change=change):
    f=Fixture(Path(self.tmp.name)/change);f.alive=False;f.forbid_push=True
    f.after_cleanup=lambda obj:setattr(obj,'metadata',(1795,99,*obj.metadata[2:])) if change=='file' else setattr(obj,'cid','c'*64)
    self.assertEqual(f.ensure_rootd_root(timeout=2)['code'],'rootd_resource_conflict');self.assertFalse(f.launches or f.pushes)
 def test_container_race_during_file_hash_fails_before_push_or_launch(self):
  f=self.f;f.alive=False;f.after_digest=lambda obj:setattr(obj,'cid','c'*64);self.assertEqual(f.ensure_rootd_root(timeout=2)['code'],'rootd_resource_conflict');self.assertFalse(f.pushes or f.launches)
 def test_new_listener_before_provision_fails_without_push_or_launch(self):
  f=self.f;f.alive=False;f.port_hook=lambda obj:setattr(obj,'listener_conflict',True) if obj.port_reads==2 else None;self.assertEqual(f.ensure_rootd_root(timeout=2)['code'],'rootd_resource_conflict');self.assertFalse(f.pushes or f.launches)
 def test_wrong_version_is_not_directly_authenticated_as_current_candidate(self):
  self.f.process['digest']='c'*64;self.assertEqual(self.auth(),'rootd_resource_conflict');self.assertFalse(self.f.probes)
 def test_fresh_daemon_unauthorized_never_becomes_success(self):
  f=self.f;f.alive=False;f.client.status={'httpStatus':401,'ok':False};self.assertEqual(f.ensure_rootd_root(timeout=2)['code'],'rootd_unauthorized');self.assertEqual(len(f.launches),1);self.assertFalse(f.probes)
 def test_file_proof_rejects_bad_sizes_and_container_selectors(self):
  f=self.f;f.alive=False
  for cid,digest,size in ((None,DIGEST,15),(CID,None,15),(CID,DIGEST,0),(CID,DIGEST,True)):
   self.assertEqual(f._rootd_deployed_candidate(cid,digest,size,deadline=10),(None,False))
if __name__=='__main__':unittest.main()
