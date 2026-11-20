"""Actual Native orphan guard/termination/ensure functions, with hostile metadata."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time
import types
import unittest

from test_proxy_observer_classification import patched_observer
SOURCE='shipped-native-backend-patch'
NAMES={'_rootd_authentication','_rootd_owned_authentication','_rootd_deployed_candidate','_recover_current_rootd_orphan','_terminate_rootd_process','_proc_start_time','ensure_rootd_root','_rootd_owned_process','_rootd_port_inodes'}
PATCHED_SOURCE=patched_observer()
tree=ast.parse(PATCHED_SOURCE);methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in NAMES]
# Reviewed complete native backend SHA256 c42b8f7cc122e6c597a44fd0cd1969d5e2b5f728e0366f7f0aa80387dd5b0ebc.
assert len(methods)==len(NAMES)
assert hashlib.sha256('\n'.join(ast.get_source_segment(PATCHED_SOURCE,n) for n in sorted(methods,key=lambda n:n.name)).encode()).hexdigest()=='96d03e60023df4a7c0af4a835fbc5c1b6fe676b8fc6e03e1296bae395c8a774d'
class_node=ast.ClassDef(name='Native',bases=[],keywords=[],body=methods,decorator_list=[])
namespace={'subprocess':__import__('subprocess'),'json':json,'Optional':__import__('typing').Optional,'Any':object,'Mapping':__import__('collections.abc',fromlist=['Mapping']).Mapping,'Path':Path,'hashlib':hashlib,'stat':stat,'re':re,'shlex':shlex,'time':time,'which':lambda name:'/usr/bin/'+name,'InstanceError':RuntimeError,'DaemonClient':object,'_ROOTD_PROVISION_TIMEOUT_SECONDS':30,'_ROOTD_REMOTE_PATH':'/data/local/tmp/xenoid-rootd','_ROOTD_LEGACY_PATH':'/data/local/tmp/.netd-helper','_ROOTD_RUN_DIRECTORY':'/data/local/tmp/xenoid-rootd-run','_ROOTD_PROCESS_RECORD':'/data/local/tmp/xenoid-rootd-run/process.json','_ROOTD_PROCESS_SCHEMA':'dev.xenoid.rootd-process/v1'}
exec(compile(ast.fix_missing_locations(ast.Module(body=[class_node],type_ignores=[])),str(SOURCE),'exec'),namespace)
Native=namespace['Native'];CID='a'*64;TOKEN='b'*32;DIGEST=hashlib.sha256(b'published-rootd').hexdigest();PID=4182;START=4740166

class Fixture(Native):
 def __init__(self):
  self.lease=types.SimpleNamespace(rootd_port=18767);self.context=types.SimpleNamespace(instance_id='immutable-uuid',resource_tag='tag')
  self.alive=True;self.record=False;self.cid=CID;self.pids=str(PID);self.start=START;self.pid=PID;self.exe='/data/local/tmp/xenoid-rootd (deleted)'
  self.directory=(1795,3670026,0,stat.S_IFDIR|0o700,2,4096)
  self.deployed=(1795,3670027,0,stat.S_IFREG|0o755,1,32432);self.running=(1795,3670025,0,stat.S_IFREG|0o755,0,32432)
  self.uid='0 0 0 0';self.arguments=['/data/local/tmp/xenoid-rootd','18767','/data/local/tmp/xenoid-rootd-run']
  self.run_digest=self.deploy_digest=DIGEST;self.inodes={'448399'};self.conflict=False;self.owners=str(PID);self.fd='4 -> socket:[448399]'
  self.auth={'ok':True,'exitCode':0,'stdout':'0\n'};self.after_auth=None;self.kills=[];self.probes=[];self.reads=[];self.launches=0;self.legacy_calls=0;self.pushes=0;self.allow_stop=True;self.write_actions=[]
 def _owned_container_record(self,timeout):return {'Id':self.cid},None
 def _rootd_output(self,args,**kwargs):
  self.reads.append(args)
  if args==['pidof','xenoid-rootd']:return self.pids if self.alive else None
  if args==['getprop','ro.product.cpu.abi']:return 'arm64-v8a'
  if args[0]=='sh':
   if 'test ! -e' in args[2]:return None if self.record else ''
   return self.owners
  if args[:2]==['ls','-l']:return self.fd
  if args[0]=='readlink':return self.exe if self.alive else None
  if args[0]=='cat':
   if args[1].endswith('/stat'):
    return f'{self.pid} (xenoid-rootd) S '+' '.join(['0']*18+[str(self.start)]) if self.alive else None
   if args[1].endswith('/status'):return 'Uid:\t'+self.uid+'\n'
   if args[1].endswith('/cmdline'):return '\0'.join(self.arguments)+'\0'
  raise AssertionError(args)
 def _rootd_stat(self,path,**kwargs):
  if path.endswith('xenoid-rootd-run'):return self.directory
  if path.startswith('/proc/'):return self.running
  return self.deployed
 def _rootd_remote_digest(self,path,**kwargs):return self.run_digest if path.startswith('/proc/') else self.deploy_digest
 def _rootd_port_inodes(self,**kwargs):return (self.inodes if self.alive else set()),self.conflict
 def _rootd_exec(self,command,token,**kwargs):
  self.probes.append((command,token))
  if self.after_auth:self.after_auth(self)
  return self.auth
 def docker_exec(self,args,**kwargs):
  self.write_actions.append(args)
  if args[0]=='kill':
   self.kills.append(args);assert args==['kill','-TERM',str(PID)]
   if self.allow_stop:self.alive=False
  return {'ok':True}
 def migrate_legacy_token_state(self):pass
 def _remove_legacy_android_rootd_token(self,*args,**kwargs):return True
 def daemon_client(self,**kwargs):return types.SimpleNamespace(read_private_token=lambda **kwargs:TOKEN)
 def _rootd_owned_process(self,**kwargs):
  return ({'pid':self.pid,'startTime':self.start,'digest':DIGEST},'valid') if self.launches else (None,'absent')
 def _adopt_legacy_rootd(self,*args,**kwargs):self.legacy_calls+=1;return False,False
 def adb(self,args,**kwargs):self.pushes+=1;return {'ok':True}
 def _launch_rootd_with_token(self,*args,**kwargs):
  self.launches+=1;self.alive=True;self.record=True;self.pid=5000;self.start=START+1;self.exe='/data/local/tmp/xenoid-rootd';self.running=self.deployed;return True
 def _rootd_authentication(self,*args,**kwargs):return 'ok'

class CurrentOrphanTests(unittest.TestCase):
 def recover(self,fixture):return fixture._recover_current_rootd_orphan(CID,DIGEST,TOKEN,deadline=time.monotonic()+10)
 def test_valid_deleted_owned_authenticated_process_is_terminated_only_once(self):
  f=Fixture();self.assertEqual(self.recover(f),(True,True));self.assertEqual(f.kills,[['kill','-TERM',str(PID)]]);self.assertEqual(f.probes,[('id -u',TOKEN)]);self.assertFalse(f.record);self.assertTrue(all('set -o pipefail' in command[2] for command in f.reads if command[0]=='sh' and 'awk -v needle' in command[2]))
 def test_valid_current_executable_orphan_also_requires_exact_same_inode(self):
  f=Fixture();f.exe='/data/local/tmp/xenoid-rootd';f.running=f.deployed;self.assertEqual(self.recover(f),(True,True))
 def test_absent_current_process_does_not_claim_orphan(self):
  f=Fixture();f.pids=None;self.assertEqual(self.recover(f),(False,False));self.assertFalse(f.kills)
 def test_no_record_is_fabricated(self):
  f=Fixture();self.recover(f);self.assertFalse(f.record);self.assertTrue(all(args[0] not in ('cp','mv','rm','echo','tee') for args in f.reads))
 def test_foreign_or_ambiguous_metadata_never_killed(self):
  variants={
   'foreign_container':{'cid':'f'*64},'existing_record':{'record':True},'multiple_current_pids':{'pids':'4182 9999'},'pid1':{'pids':'1'},
   'unsafe_directory':{'directory':(1795,1,0,stat.S_IFDIR|0o777,2,4096)},'symlink_directory':{'directory':(1795,1,0,stat.S_IFLNK|0o700,1,20)},
   'nonroot_file':{'deployed':(1795,2,2000,stat.S_IFREG|0o755,1,32432)},'writable_file':{'deployed':(1795,2,0,stat.S_IFREG|0o777,1,32432)},
   'file_hardlink':{'deployed':(1795,2,0,stat.S_IFREG|0o755,2,32432)},'foreign_uid':{'uid':'0 2000 0 0'},
   'wrong_executable':{'exe':'/data/local/tmp/foreign (deleted)'},'wrong_port':{'arguments':['/data/local/tmp/xenoid-rootd','9999','/data/local/tmp/xenoid-rootd-run']},
   'wrong_directory_arg':{'arguments':['/data/local/tmp/xenoid-rootd','18767','/other']},'extra_argument':{'arguments':['/data/local/tmp/xenoid-rootd','18767','/data/local/tmp/xenoid-rootd-run','secret']},
   'foreign_running_binary':{'run_digest':'c'*64},'foreign_deployed_binary':{'deploy_digest':'c'*64},'nondeleted_link_count':{'running':(1795,3,0,stat.S_IFREG|0o755,1,32432)},
   'different_filesystem':{'running':(9999,3,0,stat.S_IFREG|0o755,0,32432)},'different_size':{'running':(1795,3,0,stat.S_IFREG|0o755,0,123)},
   'anyaddr_listener':{'conflict':True},'multiple_listener_inodes':{'inodes':{'448399','999'}},'wrong_socket_fd':{'fd':'4 -> socket:[999]'},'shared_socket':{'owners':'4182 9999'},'unreadable_socket_owners':{'owners':None},
  }
  for name,change in variants.items():
   with self.subTest(name=name):
    f=Fixture()
    for key,value in change.items():setattr(f,key,value)
    self.assertEqual(self.recover(f),(False,True));self.assertFalse(f.kills)
 def test_current_post_authentication_is_required(self):
  for response in (None,{'ok':False,'error':'unauthorized'},{'ok':True,'exitCode':1,'stdout':'0'}, {'ok':True,'exitCode':0,'stdout':'2000'}):
   with self.subTest(response=response):
    f=Fixture();f.auth=response;self.assertEqual(self.recover(f),(False,True));self.assertFalse(f.kills)
 def test_identity_changes_between_auth_and_stop_are_rejected(self):
  for field,value in [('start',START+1),('cid','f'*64),('record',True),('inodes',{'999'}),('run_digest','f'*64),('owners','999')]:
   with self.subTest(field=field):
    f=Fixture();f.after_auth=lambda f:setattr(f,field,value);self.assertEqual(self.recover(f),(False,True));self.assertFalse(f.kills)
 def test_expired_budget_never_kills(self):
  f=Fixture();self.assertEqual(f._recover_current_rootd_orphan(CID,DIGEST,TOKEN,deadline=time.monotonic()-1),(False,True));self.assertFalse(f.kills)
 def test_actual_ensure_uses_guard_then_standard_provision_and_owned_record(self):
  f=Fixture()
  with tempfile.TemporaryDirectory() as tmp:
   candidate=Path(tmp)/'rootd';candidate.write_bytes(b'published-rootd');f._artifact_output=lambda *args:candidate
   result=f.ensure_rootd_root(timeout=10)
  self.assertTrue(result['ok']);self.assertFalse(result['reused']);self.assertEqual(f.launches,1);self.assertEqual(f.pushes,1);self.assertEqual(f.legacy_calls,0);self.assertTrue(f.record);self.assertEqual(f.pid,5000)
 def test_actual_ensure_does_not_legacy_adopt_a_rejected_current_process(self):
  f=Fixture();f.auth={'ok':False}
  with tempfile.TemporaryDirectory() as tmp:
   candidate=Path(tmp)/'rootd';candidate.write_bytes(b'published-rootd');f._artifact_output=lambda *args:candidate
   result=f.ensure_rootd_root(timeout=10)
  self.assertEqual(result['code'],'rootd_resource_conflict');self.assertEqual(f.launches,0);self.assertEqual(f.pushes,0);self.assertEqual(f.legacy_calls,0);self.assertFalse(f.kills)
 def test_actual_ensure_retains_separate_legacy_path_when_no_current_process(self):
  f=Fixture();f.pids=None
  with tempfile.TemporaryDirectory() as tmp:
   candidate=Path(tmp)/'rootd';candidate.write_bytes(b'published-rootd');f._artifact_output=lambda *args:candidate
   result=f.ensure_rootd_root(timeout=10)
  self.assertEqual(result['code'],'rootd_resource_conflict');self.assertEqual(f.legacy_calls,1);self.assertFalse(f.kills)


class ProducerFixture(Fixture):
 _rootd_owned_process=Native._rootd_owned_process
 def __init__(self):
  super().__init__();self.presence='present';self.pid_presence='present';self.pid_stat_unreadable=False;self.identity=None
 def _rootd_output(self,args,**kwargs):
  if args[0]=='sh':
   if 'then printf present; else printf absent; fi' in args[2]:
    return self.pid_presence if '/proc/' in args[2] else self.presence
  if args==['cat','/data/local/tmp/xenoid-rootd-run/process.json']:
   return json.dumps({'schema':'dev.xenoid.rootd-process/v1','pid':PID,'startTime':START})
  if args==['cat',f'/proc/{PID}/stat'] and self.pid_stat_unreadable:return None
  return super()._rootd_output(args,**kwargs)
 def _rootd_stat(self,path,**kwargs):
  if path.endswith('process.json'):return (1795,99,0,stat.S_IFREG|0o600,1,104)
  return super()._rootd_stat(path,**kwargs)
 def _rootd_process_identity(self,*args,**kwargs):return self.identity

class ProducerSafetyTests(unittest.TestCase):
 def state(self,f):return f._rootd_owned_process(deadline=time.monotonic()+10)[1]
 def test_record_probe_transport_failure_is_unknown_not_absent(self):
  f=ProducerFixture();f.presence=None;self.assertEqual(self.state(f),'invalid')
 def test_bounded_explicit_absence_remains_absent(self):
  f=ProducerFixture();f.presence='absent';self.assertEqual(self.state(f),'absent')
 def test_live_same_pid_unverifiable_identity_is_not_stale(self):
  f=ProducerFixture();self.assertEqual(self.state(f),'invalid')
 def test_unreadable_pid_stat_with_live_process_is_not_stale(self):
  f=ProducerFixture();f.pid_stat_unreadable=True;self.assertEqual(self.state(f),'invalid')
 def test_unreadable_pid_and_presence_are_not_proof_of_death(self):
  f=ProducerFixture();f.pid_stat_unreadable=True;f.pid_presence=None;self.assertEqual(self.state(f),'invalid')
 def test_proven_pid_absence_is_stale(self):
  f=ProducerFixture();f.pid_stat_unreadable=True;f.pid_presence='absent';self.assertEqual(self.state(f),'stale')
 def test_proven_reused_pid_start_is_stale(self):
  f=ProducerFixture();f.start=START+1;self.assertEqual(self.state(f),'stale')
 def test_unreadable_tcp_tables_are_conflict_not_free_port(self):
  for failed in ('/proc/net/tcp','/proc/net/tcp6'):
   with self.subTest(failed=failed):
    f=Fixture();f._rootd_output=lambda args,**kwargs:None if args[1]==failed else 'header\n'
    self.assertEqual(Native._rootd_port_inodes(f),(set(),True))
 def test_real_free_tables_can_be_empty(self):
  f=Fixture();f._rootd_output=lambda *_args,**_kwargs:'header\n';self.assertEqual(Native._rootd_port_inodes(f),(set(),False))
 def test_actual_ensure_does_not_push_remove_record_or_kill_on_unknown_identity(self):
  f=ProducerFixture()
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'rootd';path.write_bytes(b'published-rootd');f._artifact_output=lambda *_args:path
   result=f.ensure_rootd_root(timeout=10)
  self.assertEqual(result['code'],'rootd_resource_conflict');self.assertEqual(f.pushes,0);self.assertEqual(f.write_actions,[]);self.assertFalse(f.kills)
 def test_actual_ensure_does_not_push_remove_record_or_kill_on_unknown_record_presence(self):
  f=ProducerFixture();f.presence=None
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'rootd';path.write_bytes(b'published-rootd');f._artifact_output=lambda *_args:path
   result=f.ensure_rootd_root(timeout=10)
  self.assertEqual(result['code'],'rootd_resource_conflict');self.assertEqual(f.pushes,0);self.assertEqual(f.write_actions,[])
 def test_actual_ensure_does_not_push_or_kill_on_unknown_port(self):
  f=Fixture();f._rootd_port_inodes=lambda **_kwargs:(set(),True)
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'rootd';path.write_bytes(b'published-rootd');f._artifact_output=lambda *_args:path
   result=f.ensure_rootd_root(timeout=10)
  self.assertEqual(result['code'],'rootd_resource_conflict');self.assertEqual(f.pushes,0);self.assertEqual(f.write_actions,[])

if __name__=='__main__':unittest.main()
