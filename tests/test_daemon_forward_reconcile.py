"""Actual backend method regressions; modeled ADB/Docker, no host actions."""
import ast,copy,math,re,time,types,unittest
from pathlib import Path
from typing import Any,Mapping,Optional
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
SOURCE=HERE.parent/'nativebundle/source/src/xenoid/backend.py'
module=ast.parse(SOURCE.read_text());cls=next(n for n in module.body if isinstance(n,ast.ClassDef)and n.name=='RuntimeManager')
names={'forward_daemon_port','wait_daemon_transport'}
ns={'Any':Any,'Optional':Optional,'Mapping':Mapping,'math':math,'re':re,'time':types.SimpleNamespace(),
    '_DAEMON_TRANSPORT_TIMEOUT_SECONDS':30.0,'_DAEMON_START_TRANSPORT_TIMEOUT_SECONDS':120.0}
exec(compile(ast.Module(body=[n for n in cls.body if isinstance(n,ast.FunctionDef)and n.name in names],type_ignores=[]),str(SOURCE),'exec'),ns)
CID='1'*64
class Cancelled(Exception):pass
class Model:
 def __init__(self,rows=None):
  self.now=0.;self.events=[];self.rows=copy.deepcopy(rows or []);self.online=True;self.ready=True;self.lost=False;self.owner_changed=False;self.create_error=False;self.race=None;self.parent=None;self.cancel=False;self.cost=.01;self.no_owner_calls=0
  self.lease=types.SimpleNamespace(host_daemon_port=18767,android_daemon_port=18765);self.adb_target='127.0.0.1:5557'
  self.forward_daemon_port=types.MethodType(ns['forward_daemon_port'],self)
  self.wait_daemon_transport=types.MethodType(ns['wait_daemon_transport'],self)
 def _owned_container_record(self,**kw):
  self.no_owner_calls+=1
  return({'Id':CID,'State':{'Running':True,'Pid':100 if not self.owner_changed else 101,'StartedAt':'one'if not self.owner_changed else'two'}},None)
 def adb(self,args,timeout=None):
  self.events.append((tuple(args),timeout,self.now));self.now+=min(self.cost,timeout)
  if self.cancel:raise Cancelled()
  if args==['get-state']:return{'ok':self.online,'stdout':'device\n'if self.online else'offline\n'}
  if args==['connect']:
   self.online=True;return{'ok':True}
  if args==['forward','--list']:return{'ok':True,'stdout':''.join(' '.join(r)+'\n'for r in self.rows)}
  assert args[:2]==['forward','--no-rebind'];assert tuple(args[2:])==('tcp:18767','tcp:18765')
  if self.race:
   self.rows=[self.race];return{'ok':False,'stderr':'cannot rebind'}
  if self.create_error or not self.online:return{'ok':False,'stderr':'offline'}
  assert not any(r[1]=='tcp:18767'for r in self.rows),'must never overwrite a row'
  self.rows.append([self.adb_target,*args[2:]]);return{'ok':True}
 def daemon_client(self,**kw):return types.SimpleNamespace(transport=self.transport)
 def transport(self,**kw):
  if self.lost:
   self.lost=False;self.rows=[];self.online=False;return{'ok':False,'error':'connection_refused'}
  return{'ok':self.ready and self.online and self.rows==[[self.adb_target,'tcp:18767','tcp:18765']],'error':'connection_refused'}
 def launch_daemon_activity_once(self,**kw):raise AssertionError('ordinary transport must not launch Activity')
 def context(self):
  return patch.dict(ns,{'time':types.SimpleNamespace(monotonic=lambda:self.now,sleep=lambda s:setattr(self,'now',self.now+s)),
   'bounded_timeout':lambda v:v if self.parent is None else min(v,self.parent)})
 def create_count(self):return sum(x[0][:2]==('forward','--no-rebind')for x in self.events)
GOOD=['127.0.0.1:5557','tcp:18767','tcp:18765']
class DaemonForward(unittest.TestCase):
 def test_initially_absent_forward_is_created_and_host_HTTP_proven(self):
  m=Model()
  with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertTrue(r['ok']);self.assertEqual(m.create_count(),1);self.assertFalse(any(e[0]==('connect',)for e in m.events))
 def test_forward_lost_on_transport_reconnect_is_restored_in_same_wait(self):
  m=Model([GOOD]);m.lost=True
  with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertTrue(r['ok']);self.assertEqual(m.create_count(),1);self.assertIn(('connect',),[e[0]for e in m.events]);self.assertEqual(m.rows,[GOOD])
 def test_initial_offline_forward_failure_recovers_without_another_UP(self):
  m=Model();m.online=False
  with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertTrue(r['ok']);self.assertEqual(m.rows,[GOOD]);self.assertIn(('connect',),[e[0]for e in m.events])
 def test_warm_mapping_is_noop_not_rebound_or_reconnected(self):
  m=Model([GOOD])
  with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertTrue(r['ok']);self.assertEqual([e[0]for e in m.events],[('forward','--list')]*2);self.assertEqual(m.create_count(),0)
 def test_HTTP_ready_without_proven_forward_is_never_accepted(self):
  m=Model();m.create_error=True;calls=[]
  m.transport=lambda**kw:(calls.append(1)or{'ok':True})
  with m.context():r=m.wait_daemon_transport(2,allow_activity_launch=False)
  self.assertFalse(r['ok']);self.assertEqual(calls,[]);self.assertEqual(m.rows,[])
 def test_forward_changed_during_ready_HTTP_rejects_foreign_or_reproves_repair(self):
  for foreign in(False,True):
   m=Model([GOOD]);calls=[]
   def response(**kw):
    calls.append(1)
    if len(calls)==1:m.rows=[['other','tcp:18767','tcp:18765']]if foreign else[]
    return{'ok':True}
   m.transport=response
   with m.context():r=m.wait_daemon_transport(2,allow_activity_launch=False)
   if foreign:self.assertFalse(r['ok']);self.assertEqual(r['error'],'daemon_forward_conflict');self.assertEqual(len(calls),1)
   else:self.assertTrue(r['ok']);self.assertEqual(len(calls),2);self.assertEqual(m.create_count(),1)
 def test_foreign_serial_remote_or_duplicate_mapping_rejected_without_overwrite(self):
  for rows in([[['127.0.0.1:5556','tcp:18767','tcp:18765']],[['127.0.0.1:5557','tcp:18767','tcp:9999']],[GOOD,GOOD]]):
   m=Model(rows)
   with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
   self.assertEqual(r['error'],'daemon_forward_conflict');self.assertEqual(m.rows,rows);self.assertEqual(m.create_count(),0)
 def test_expected_CID_and_live_incarnation_changes_fail_before_new_forward(self):
  m=Model()
  with m.context():r=m.forward_daemon_port(expected_container_id='2'*64)
  self.assertEqual(r['error'],'daemon_transport_owner_changed');self.assertEqual(m.events,[])
  m=Model();original=m.adb
  def changed(args,**kw):r=original(args,**kw);m.owner_changed=True;return r
  m.adb=changed
  with m.context():r=m.forward_daemon_port()
  self.assertEqual(r['error'],'daemon_transport_owner_changed');self.assertEqual(m.create_count(),0)
 def test_concurrent_exact_lease_create_race_accepted_but_foreign_race_rejected(self):
  for row,success in[(GOOD,True),(['other','tcp:18767','tcp:18765'],False)]:
   m=Model();m.race=row
   with m.context():r=m.forward_daemon_port()
   self.assertEqual(r['ok'],success);self.assertEqual(m.rows,[row])
 def test_transient_forward_failure_is_not_fake_ready_and_preserves_cause(self):
  m=Model();m.create_error=True
  with m.context():r=m.wait_daemon_transport(2,allow_activity_launch=False)
  self.assertFalse(r['ok']);self.assertEqual(r['error'],'daemon_transport_timeout');self.assertEqual(r['lastForwardCode'],'daemon_forward_failed');self.assertLessEqual(m.now,2)
 def test_parent_deadline_caps_all_ADB_commands_and_zero_budget_does_nothing(self):
  m=Model();m.create_error=True;m.parent=.2
  with m.context():r=m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertFalse(r['ok']);self.assertLessEqual(m.now,.2);self.assertTrue(all(0<t<=.2-at+1e-9 for _,t,at in m.events))
  m=Model()
  with m.context():r=m.wait_daemon_transport(0,allow_activity_launch=False)
  self.assertFalse(r['ok']);self.assertEqual(m.events,[]);self.assertEqual(m.no_owner_calls,0)
 def test_owner_change_in_get_state_rejects_before_connect(self):
  m=Model();m.online=False;old=m.adb
  def changed(args,**kw):
   r=old(args,**kw)
   if args==['get-state']:m.owner_changed=True
   return r
  m.adb=changed
  with m.context():r=m.wait_daemon_transport(2,allow_activity_launch=False)
  self.assertFalse(r['ok']);self.assertEqual(r['error'],'daemon_transport_owner_changed');self.assertNotIn(('connect',),[e[0]for e in m.events])
 def test_owner_lookup_exhausting_budget_dispatches_no_ADB_command(self):
  m=Model()
  old=m._owned_container_record
  def slow(**kw):m.now+=kw['timeout'];return old(**kw)
  m._owned_container_record=slow
  with m.context():r=m.forward_daemon_port(.05)
  self.assertFalse(r['ok']);self.assertEqual(r['error'],'daemon_transport_timeout');self.assertEqual(m.events,[])
 def test_cancellation_is_not_swallowed_or_retried(self):
  m=Model();m.cancel=True
  with m.context(),self.assertRaises(Cancelled):m.wait_daemon_transport(120,allow_activity_launch=False)
  self.assertEqual(len(m.events),1)
 def test_actual_start_transport_tail_recovers_first_forward_race_but_rejects_conflict(self):
  start=next(n for n in cls.body if isinstance(n,ast.FunctionDef)and n.name=='start_owned_container')
  offset=next(i for i,n in enumerate(start.body)if isinstance(n,ast.Assign)and any(isinstance(t,ast.Subscript)and isinstance(t.value,ast.Name)and t.value.id=='result'and isinstance(t.slice,ast.Constant)and t.slice.value=='daemonForward'for t in n.targets))
  # Execute the unchanged actual production start tail from its first forward
  # through storage/transport acceptance. Model only storage and OS transports.
  args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=n)for n in('self','result','deadline','container_id')],kwonlyargs=[],kw_defaults=[],defaults=[])
  f=ast.FunctionDef(name='start_tail',args=args,body=start.body[offset:],decorator_list=[])
  tree=ast.fix_missing_locations(ast.Module(body=[f],type_ignores=[]));env={**ns,'StorageStateStore':lambda*args:types.SimpleNamespace(load=lambda:{'state':'committed'}),'StorageError':RuntimeError,'allow_daemon_activity_launch':False}
  exec(compile(tree,str(SOURCE),'exec'),env)
  for foreign in(False,True):
   m=Model([['other','tcp:18767','tcp:18765']]if foreign else[]);m.online=False
   with m.context(),patch.dict(env,{'time':ns['time']}):r=env['start_tail'](m,{'ok':True},300,CID)
   if foreign:
    self.assertFalse(r['ok']);self.assertNotIn('daemonTransport',r);self.assertEqual(m.create_count(),0)
   else:
    self.assertTrue(r['ok']);self.assertFalse(r['daemonForward']['ok']);self.assertTrue(r['daemonTransport']['ok']);self.assertEqual(m.rows,[GOOD])
if __name__=='__main__':unittest.main()
