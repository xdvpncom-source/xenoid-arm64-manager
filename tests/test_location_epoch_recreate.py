"""Portable tests of the shipped planner/executor and native location method.

The engine and daemon are doubles. No external native checkout, Docker or ADB is
used; shipped native fixture definitions and the production functions are read
as source. The earlier private qualification also exercises real native stores.
"""
import ast
import base64
import contextlib
from contextvars import ContextVar
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
NATIVE=ROOT/'nativebundle/source'
PKG='epoch39_fixture'
package=ModuleType(PKG);package.__path__=[];sys.modules[PKG]=package
config=ModuleType(PKG+'.config')
class InstanceError(RuntimeError):pass
config.InstanceError=InstanceError;sys.modules[config.__name__]=config
lock=ModuleType(PKG+'.operation_lock');lock.__package__=PKG
lock_tree=ast.parse((ROOT/'tests/test_qa_native_lock.py').read_text())
lock_source=next(ast.literal_eval(n.value)for n in lock_tree.body if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='NATIVE_SOURCE'for t in n.targets))
exec(compile(lock_source,'pinned-operation-lock','exec'),lock.__dict__);sys.modules[lock.__name__]=lock
util=ModuleType(PKG+'.util');util.__dict__.update(contextmanager=contextlib.contextmanager,Optional=__import__('typing').Optional,time=__import__('time'))
util._COMMAND_DEADLINE=ContextVar('epoch39_command_deadline',default=None)
@contextlib.contextmanager
def command_timeout(seconds):
    if seconds<=0:raise ValueError('command_timeout_invalid')
    deadline=__import__('time').monotonic()+seconds;old=util._COMMAND_DEADLINE.get()
    token=util._COMMAND_DEADLINE.set(min(deadline,old)if old is not None else deadline)
    try:yield
    finally:util._COMMAND_DEADLINE.reset(token)
def bounded_timeout(timeout=None):
    value=util._COMMAND_DEADLINE.get()
    if value is None:return timeout
    remaining=max(.001,value-__import__('time').monotonic())
    return remaining if timeout is None else min(float(timeout),remaining)
util.command_timeout=command_timeout;util.bounded_timeout=bounded_timeout;sys.modules[util.__name__]=util

def load_convergence(name,source):
    module=ModuleType(PKG+'.'+name);module.__package__=PKG;sys.modules[module.__name__]=module
    exec(compile(source,str(NATIVE/'src/xenoid/convergence.py'),'exec'),module.__dict__);return module
source=(NATIVE/'src/xenoid/convergence.py').read_text();c=load_convergence('convergence',source)

# Reuse only the shipped, runtime-free manager/acceptance fixtures, not their
# import-time native modules or command-line runner.
tree=ast.parse((NATIVE/'scripts/test-convergence.py').read_text())
keep={'ContractFailure','require','artifact_record','selected_image','healthy_snapshot','FakeManager','FakeArtifacts','FakeAcceptance','manager_fixture','planner_for'}
nodes=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]
for node in tree.body:
    if isinstance(node,ast.Assign):
        if not isinstance(node.value,(ast.Constant,ast.Tuple,ast.List,ast.Set,ast.Dict,ast.BinOp)):continue
        if any(isinstance(v,(ast.Call,ast.Attribute))for v in ast.walk(node.value)):continue
        nodes.append(node)
    elif isinstance(node,(ast.FunctionDef,ast.ClassDef))and node.name in keep:nodes.append(node)
f={'convergence':c,'contextlib':contextlib,'copy':copy,'Path':Path,'SimpleNamespace':SimpleNamespace,'tempfile':tempfile,'time':__import__('time'),'Mapping':__import__('collections.abc').abc.Mapping,'microg_google_status':lambda:{'ok':True}}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'shipped-convergence-fixtures','exec'),f)

location=ModuleType(PKG+'.location')
class LocationError(ValueError):pass
location.LocationError=LocationError
location.location_runtime_epoch=lambda cid:hashlib.sha256(('xenoid-location-epoch/v1:'+cid).encode()).hexdigest()
class LocationStateStore:
    def __init__(self,root,dns=None):self.path=Path(root)/'location-fixture.json'
    def load(self):return json.loads(self.path.read_bytes())if self.path.exists()else None
    def save(self,state):self.path.write_text(json.dumps(state))
    def ensure(self,iid):
        state={'instanceId':iid,'profiles':{'SG':{'profileDigest':'a'*64,'profile':{'identityDigest':'a'*64}}},'simEpoch':'','pending':None,'active':None}
        self.save(state);return state,True
    def target_profile(self,state):return state['profiles']['SG']['profile']
    def mark_validated(self,epoch):
        state=self.load();state['active']['lastValidatedRuntimeEpoch']=epoch;self.save(state);return state
location.LocationStateStore=LocationStateStore
location.generate_cellular_profile=lambda *a,**k:None
location.public_summary=lambda state:{'active':copy.deepcopy(state['active'])}
location.masked_android_status=lambda state:copy.deepcopy(state)
def convergence_action(state,android,epoch):
    if android.get('profileDigest')==state['active']['profileDigest']and android.get('runtimeEpoch')==epoch:return {'step':'noop'}
    return {'step':'verify','restage':False,'promote':False}
location.convergence_action=convergence_action;location.STAGE_SCHEMA='dev.xenoid.location-stage/v1';sys.modules[location.__name__]=location
cellular=ModuleType(PKG+'.cellular')
def no_stage(*args):raise AssertionError('no cellular serialization or profile replacement expected')
cellular.encode_profile_v1=no_stage;sys.modules[cellular.__name__]=cellular
backend=ast.parse((NATIVE/'src/xenoid/backend.py').read_text())
manager=next(n for n in backend.body if isinstance(n,ast.ClassDef)and n.name=='RuntimeManager')
methods=[]
for node in manager.body:
    if isinstance(node,ast.FunctionDef)and node.name in {'_convergence_action_name','reconcile_location'}:
        node=copy.deepcopy(node);node.decorator_list=[];methods.append(node)
scope={'__package__':PKG,'InstanceError':InstanceError,'Mapping':__import__('collections.abc').abc.Mapping,'base64':base64}
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*methods],type_ignores=[])),'actual-native-location-method','exec'),scope)
RuntimeManager=SimpleNamespace(_convergence_action_name=scope['_convergence_action_name'],reconcile_location=scope['reconcile_location'])

def active_snapshot():
    snapshot=f['healthy_snapshot']();snapshot['components']['location']['host']={'active':{'country':'SG','profileDigest':'a'*64}}
    return snapshot

class EpochManager(f['FakeManager']):
    _convergence_action_name=staticmethod(RuntimeManager._convergence_action_name)
    def __init__(self,root,snapshot):
        super().__init__(root,snapshot);self.cfg=SimpleNamespace(network_dns_servers=['1.1.1.1','8.8.8.8'])
        self.store=location.LocationStateStore(root,self.cfg.network_dns_servers);state,_=self.store.ensure(f['INSTANCE_ID'])
        digest=state['profiles']['SG']['profileDigest'];epoch=location.location_runtime_epoch(f['OLD_CONTAINER'])
        state['active']={'country':'SG','profileDigest':digest,'appliedAt':1,'lastValidatedRuntimeEpoch':epoch,'simEpoch':''}
        self.store.save(state);self.daemon={'ok':True,'state':'active','profileDigest':digest,'runtimeEpoch':epoch};self.verify_calls=[]
        self.snapshot['components']['location']['host']=location.public_summary(self.store.load())
    def location_runtime_container_id(self):return self.snapshot['runtime']['containerId']
    def converge_storage(self,boot_seed_target,regeneration_capability,expected_data_uuid=None,expected_rootfs_uuid=None):
        answer=super().converge_storage(boot_seed_target,regeneration_capability)
        assert expected_data_uuid in (None,answer['dataUuid']) and expected_rootfs_uuid in (None,answer['rootfsUuid'])
        return answer
    def daemon_client(self):return self
    def location_status(self,timeout=None):return copy.deepcopy(self.daemon)
    def location_stage(self,*a):raise AssertionError('existing profile must not be staged or regenerated')
    def location_verify(self,digest,epoch):
        self.verify_calls.append((digest,epoch));self.daemon['runtimeEpoch']=epoch
        return {'ok':True,'verified':True}
    def reconcile_location(self,action,regeneration_capability):
        self.calls.append(('location',action))
        answer=RuntimeManager.reconcile_location(self,action,regeneration_capability)
        self.snapshot['components']['location']['host']=location.public_summary(self.store.load())
        return answer

class Acceptance(f['FakeAcceptance']):
    def observe(self,context,expected,mode,deadline,progress):
        out=super().observe(context,expected,mode,deadline,progress)
        if mode=='convergence-resolve':
            original=expected['componentActions'];actions={}
            for key,value in original.items():
                actions[key]=({}if key=='deploy'else'reuse')if value in (None,'inspect')else value
            out['componentActions']=actions
        return out

class LocationEpoch(unittest.TestCase):
    def test_original_executor_reproduces_success_with_stale_location_epoch(self):
        start=source.index('        if (\n            runtime_action in {"create", "recreate"}',source.index('        location_host = location_component.get("host")'))
        end=source.index('        location_pending = (',start)
        old=load_convergence('original_convergence',source[:start]+source[end:])
        snap=active_snapshot();snap['runtime']['state']='stopped';snap['runtime']['createSpecMatches']=False
        with tempfile.TemporaryDirectory()as d:
            m=EpochManager(Path(d)/'state',snap)
            result=old.ConvergenceExecutor(m,live_acceptance=Acceptance(m),artifact_builder=f['FakeArtifacts'](m)).run()
            self.assertTrue(result['ok'],result);self.assertEqual(m.verify_calls,[])
            self.assertEqual(m.store.load()['active']['lastValidatedRuntimeEpoch'],location.location_runtime_epoch(f['OLD_CONTAINER']))
            self.assertNotEqual(m.store.load()['active']['lastValidatedRuntimeEpoch'],location.location_runtime_epoch(f['NEW_CONTAINER']))

    def test_actual_planner_replacement_verifies_active_epoch_but_healthy_reuse_stays_reuse(self):
        healthy=active_snapshot();self.assertEqual(f['planner_for'](healthy).location_action,'reuse')
        for state in ('running','stopped','absent'):
            changed=copy.deepcopy(healthy);changed['runtime']['state']=state
            if state=='absent':changed['runtime']['containerId']=None
            else:changed['runtime']['createSpecMatches']=False
            plan=f['planner_for'](changed)
            self.assertIn(plan.runtime_action,('create','recreate'));self.assertEqual(plan.location_action,'converge')

    def test_actual_executor_and_native_location_owner_update_only_container_epoch(self):
        snap=active_snapshot();snap['runtime']['state']='stopped';snap['runtime']['createSpecMatches']=False
        with tempfile.TemporaryDirectory()as d:
            m=EpochManager(Path(d)/'state',snap);before=m.store.load()
            with patch.object(location,'generate_cellular_profile',side_effect=AssertionError('no profile generation')):
                result=c.ConvergenceExecutor(m,live_acceptance=Acceptance(m),artifact_builder=f['FakeArtifacts'](m)).run()
            self.assertTrue(result['ok'],result);after=m.store.load()
            epoch=location.location_runtime_epoch(f['NEW_CONTAINER'])
            self.assertEqual(m.verify_calls,[(before['active']['profileDigest'],epoch)])
            self.assertEqual(after['profiles'],before['profiles']);self.assertEqual(after['simEpoch'],before['simEpoch'])
            self.assertEqual(after['active']['lastValidatedRuntimeEpoch'],epoch);self.assertIsNone(after['pending'])
            self.assertEqual(result['before']['dataUuid'],result['after']['dataUuid']);self.assertEqual(result['before']['rootfsUuid'],result['after']['rootfsUuid'])
            self.assertEqual([v for k,v in m.calls if k=='location'],['converge'])
            self.assertEqual(len([v for k,v in m.calls if k=='remove']),1)
            self.assertEqual(result['plan']['bootSeedAction'],'none')

    def test_healthy_current_runtime_does_not_verify_or_mutate_location(self):
        with tempfile.TemporaryDirectory()as d:
            m=EpochManager(Path(d)/'state',active_snapshot());before=m.store.path.read_bytes()
            result=c.ConvergenceExecutor(m,live_acceptance=Acceptance(m),artifact_builder=f['FakeArtifacts'](m)).run()
            self.assertTrue(result['ok'],result);self.assertEqual(m.verify_calls,[]);self.assertEqual(m.store.path.read_bytes(),before)

    def test_pending_location_retains_existing_one_replacement_path(self):
        snapshot=active_snapshot();snapshot['components']['location']['state']='pending'
        snapshot['components']['location']['host']['pending']={'phase':'armed'}
        plan=f['planner_for'](snapshot);self.assertEqual(plan.runtime_action,'recreate');self.assertEqual(plan.location_action,'converge')
        self.assertIn('location_pending_recreate',plan.recreate_reasons)

if __name__=='__main__':unittest.main()
