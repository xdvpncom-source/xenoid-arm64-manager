#!/usr/bin/env python3
"""Proxy retry policy regressions; mocks all process, network and route changes."""
import copy,hashlib,importlib.util,io,json,pathlib,sys,tempfile,types,unittest
from contextlib import redirect_stdout
from contextlib import ExitStack
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.dont_write_bytecode=True
POLICY={'schema':'dev.xenoid.proxy-failure-policy/v1','mode':'retry_proxy','retry_delays_seconds':[5,15,60]}
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
class Harness:
 def __init__(self,mode=True,steps=None,fail_probes=False,death_step=None,transport_step=None,compile_step=None,policy=None):
  self.m=load('fixture_agent_'+str(id(self)),'xenoid-proxy-agent.py');self.events=[];self.now=1000.;self.step=-1;self.mode=mode;self.steps=steps or[(1000,1,False),(1020,1,False),(1310,1,False)];self.phase='quarantined';self.struct=False;self.fail_probes=fail_probes;self.death_step=death_step;self.transport_step=transport_step;self.compile_step=compile_step;self.transport_done=False;self.dead=False;self.policy=copy.deepcopy(policy or POLICY)
  self.source={'kind':'endpoint','value':'socks5://proxy.example:1080','selectedNode':'node','udpAllowed':False,'allowInsecureHttp':False,'ipv6Allowed':True}
  if mode:self.source['failurePolicy']=copy.deepcopy(self.policy)
 def desired(self,*args):
  self.step+=1
  if self.transport_step==self.step and not self.transport_done:self.transport_done=True;raise self.m.TransientTransportFailure('engine_unavailable')
  if self.step>=len(self.steps):self.m._STOP=True;clock,check,diag=self.steps[-1];clock+=1;diag=False
  else:clock,check,diag=self.steps[self.step]
  self.now=clock
  return {'generation':1,'checkId':check,'enabled':True,'diagnosticRequested':diag,'source':copy.deepcopy(self.source)}
 def live(self,*args,lightweight=False):
  self.events.append((self.step,'liveness'if lightweight else'full_status'))
  if self.death_step==self.step and not self.dead:
   self.dead=True;self.struct=False;raise self.m.AgentFailure('engine_process_failed')
  return {'generation':1,'phase':self.phase,'structuralApplied':self.struct,'dataPlaneVerified':False,'capabilities':{k:False for k in self.m.CAPABILITY_KEYS},'counters':{},'selectedNode':'node','nodeCount':1,'nodes':['node']}
 def root(self,action,*args):
  self.events.append((self.step,action))
  if action=='apply':self.phase='active';self.struct=True
 def quarantine(self,*args):self.events.append((self.step,'quarantine'));self.phase='quarantined';self.struct=False
 def probe(self,*args,**kwargs):
  self.events.append((self.step,'probe'))
  if self.fail_probes:raise self.m.AgentFailure('probe_failed')
  d=self.live();d.update(dataPlaneVerified=True);return d
 def compile(self,*a):
  self.events.append((self.step,'compile'))
  if self.compile_step==self.step:raise self.m.AgentFailure('source_fetch_denied')
  return {'config':{'unchanged':True},'nodeNames':['node']}
 def report(self,channel,desired,live,**kw):self.events.append((self.step,'report',copy.deepcopy(live),kw.get('error_code','')))
 def run(self):
  h=self
  class Clock:
   @staticmethod
   def monotonic():return h.now
   @staticmethod
   def sleep(seconds):h.now+=seconds
  class Channel:
   def __init__(self,*a):pass
   def close(self):pass
  m=self.m;m._STOP=False
  mocks={'time':Clock,'DaemonChannel':Channel,'_desired':self.desired,'_engine':lambda:types.SimpleNamespace(generation=1),'_write_health':lambda *a:None,'_live_status':self.live,'_run_root':self.root,'_quarantine':self.quarantine,'_validate_binary':lambda*a:None,'_compile_source':self.compile,'apply_proxy_routing_config':lambda c,p:c,'load_proxy_routing_policy':lambda:{'failure_policy':copy.deepcopy(self.policy)}if self.mode else{},'_report_staging':lambda*a:None,'_probe_data_plane':self.probe,'_commit_candidate':lambda:self.events.append((self.step,'commit')),'_report_status':self.report,'_report_failure':lambda*a,**kw:self.events.append((self.step,'failure_report')),'_rollback_previous':lambda:self.events.append((self.step,'rollback')),'_sleep_stoppable':lambda*a:None}
  with ExitStack()as stack:
   for name,value in mocks.items():stack.enter_context(patch.object(m,name,value))
   return m._run({'generation':1,'engine':{'binarySha256':'0'*64},'instanceId':'fixture','resourceTag':'tag','runtimeEpoch':'fixture','manifestDigest':'0'*64},pathlib.Path('/never-used'),bytearray(b'a'),bytearray(b'b'),{'generation':1,'structuralApplied':False})
 def actions(self,name):return[e for e in self.events if e[1]==name]
class AgentFlow(unittest.TestCase):
 def test_retry_mode_initial_and_refresh_never_probe_external(self):
  h=Harness(fail_probes=True);self.assertEqual(h.run(),0)
  self.assertEqual(len(h.actions('apply')),1);self.assertEqual(len(h.actions('commit')),1);self.assertFalse(h.actions('probe'));self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'))
  for _,_,r,_ in h.actions('report'):
   self.assertTrue(r['structuralApplied']);self.assertFalse(r['dataPlaneVerified']);self.assertEqual(r['failurePolicy'],'retry_proxy');self.assertEqual(r['upstreamState'],'unknown')
 def test_legacy_absence_still_requires_external_proof(self):
  h=Harness(mode=False,steps=[(1000,1,False)]);h.run();self.assertEqual(len(h.actions('probe')),1)
  r=h.actions('report')[0][2];self.assertNotIn('failurePolicy',r);self.assertTrue(r['dataPlaneVerified'])
 def test_manual_outage_is_diagnostic_only_once(self):
  h=Harness(steps=[(1000,1,False),(1020,2,True),(1030,2,False),(1320,2,False)],fail_probes=True);h.run()
  self.assertEqual(len(h.actions('apply')),1);self.assertEqual(len(h.actions('probe')),1);self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'))
  failed=[r for _,_,r,e in h.actions('report')if e=='probe_failed'];self.assertEqual(len(failed),1);self.assertTrue(failed[0]['structuralApplied']);self.assertFalse(failed[0]['dataPlaneVerified']);self.assertEqual(failed[0]['upstreamState'],'retrying')
 def test_manual_success_is_real_online_proof(self):
  h=Harness(steps=[(1000,1,False),(1020,2,True)]);h.run();r=h.actions('report')[-1][2];self.assertEqual(r['upstreamState'],'online');self.assertTrue(r['dataPlaneVerified']);self.assertEqual(len(h.actions('probe')),1)
 def test_owned_local_process_death_repairs_without_probe(self):
  h=Harness(steps=[(1000,1,False),(1020,1,False),(1026,1,False),(1040,1,False)],death_step=1);h.run()
  self.assertEqual(len(h.actions('apply')),2);self.assertEqual(len(h.actions('commit')),2);self.assertFalse(h.actions('probe'));self.assertFalse(h.actions('quarantine'))
 def test_control_transport_recovery_never_reprobes_or_reapplies(self):
  h=Harness(steps=[(1000,1,False),(1010,1,False),(1020,1,False),(1040,1,False)],transport_step=1);h.run()
  self.assertEqual(len(h.actions('apply')),1);self.assertFalse(h.actions('probe'));self.assertFalse(h.actions('quarantine'));self.assertGreaterEqual(len(h.actions('report')),2)
 def test_subscription_refresh_failure_keeps_known_same_generation(self):
  h=Harness(steps=[(1000,1,False),(1310,1,False),(1316,1,False)],compile_step=1);h.run();self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'));self.assertEqual(len(h.actions('apply')),1)
  self.assertTrue(any(e=='source_fetch_denied'and r['structuralApplied']for _,_,r,e in h.actions('report')))
 def test_policy_digest_binds_full_desired_policy(self):
  h=Harness(steps=[(1000,1,False)]);h.run();r=h.actions('report')[0][2];expected=hashlib.sha256(json.dumps(POLICY,sort_keys=True,separators=(',',':')).encode()).hexdigest();self.assertEqual(r['failurePolicyDigest'],expected)
class AgentFailureBoundaries(unittest.TestCase):
 def test_long_outage_never_reapplies_or_automatically_reprobes(self):
  steps=[(1000+i*3,2 if i else 1,i==1)for i in range(150)]
  h=Harness(steps=steps,fail_probes=True);h.run();self.assertEqual(len(h.actions('probe')),1);self.assertEqual(len(h.actions('apply')),1);self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'));self.assertFalse(any(r['dataPlaneVerified']for _,_,r,_ in h.actions('report')))
 def test_lost_signed_report_after_commit_preserves_owned_process(self):
  h=Harness(steps=[(1000,1,False),(1020,1,False),(1040,1,False)]);original=h.report;lost=[False]
  def report(*args,**kw):
   if not lost[0]:lost[0]=True;raise h.m.TransientTransportFailure('agent_channel_unreachable')
   return original(*args,**kw)
  h.report=report;h.run();self.assertEqual(len(h.actions('commit')),1);self.assertEqual(len(h.actions('apply')),1);self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'));self.assertFalse(h.actions('probe'))
 def test_live_process_identity_mismatch_is_never_repaired_as_absence(self):
  h=Harness(steps=[(1000,1,False),(1020,1,False)]);old=h.live
  def live(*a,**kw):
   if kw.get('lightweight'):raise h.m.AgentFailure('process_identity_mismatch')
   return old(*a,**kw)
  h.live=live
  with self.assertRaises(h.m.AgentFailure)as caught:h.run()
  self.assertEqual(caught.exception.code,'process_identity_mismatch');self.assertEqual(len(h.actions('apply')),1)
 def test_manual_counter_or_structure_failure_keeps_strict_guard(self):
  h=Harness(steps=[(1000,1,False),(1020,2,True)])
  def probe(*a,**kw):raise h.m.AgentFailure('data_plane_unverified')
  h.probe=probe;h.run();self.assertTrue(h.actions('quarantine'));self.assertFalse(any(r.get('dataPlaneVerified')for _,_,r,_ in h.actions('report')))
 def test_liveness_is_bounded_not_fullstatus_each_control_poll(self):
  h=Harness(steps=[(1000+3*i,1,False)for i in range(11)]);h.run();self.assertEqual(len(h.actions('full_status')),2);self.assertLessEqual(len(h.actions('liveness')),2)
 def test_partial_manual_capabilities_are_retained_truthfully(self):
  m=load('partial_capability_fixture','xenoid-proxy-agent.py');caps={k:k=='v4TcpProxy'for k in m.CAPABILITY_KEYS};channel=types.SimpleNamespace(call=lambda*a:{'checkId':2,'capabilities':caps,'elapsedMs':100,'errorCode':'probe_failed'})
  desired={'checkId':2,'generation':1,'source':{'udpAllowed':False,'ipv6Allowed':True}};before={'phase':'active','structuralApplied':True,'capabilities':{k:False for k in caps},'counters':{}}
  result,error=m._continuous_diagnostic(channel,desired,{},pathlib.Path('/unused'),before,POLICY);self.assertEqual(error,'probe_failed');self.assertEqual(result['capabilities'],caps);self.assertFalse(result['dataPlaneVerified']);self.assertEqual(result['upstreamState'],'retrying')
 def test_stale_generation_remains_authentication_failure(self):
  m=load('stale_generation_fixture','xenoid-proxy-agent.py');value={'schemaVersion':2,'instanceId':'fixture','generation':0,'enabled':True,'checkId':1,'source':None,'diagnosticRequested':False};channel=types.SimpleNamespace(call=lambda*a:value)
  with self.assertRaises(m.ChannelAuthenticationFailure):m._desired(channel,{'instanceId':'fixture'},1)

class CustomRetrySchedule(unittest.TestCase):
 def policy(self):return {'schema':POLICY['schema'],'mode':'retry_proxy','retry_delays_seconds':[7,31,777]}
 def test_general_local_apply_failure_uses_every_custom_delay(self):
  h=Harness(policy=self.policy(),steps=[(1000,1,False),(1006,1,False),(1008,1,False),(1038,1,False),(1040,1,False),(1816,1,False),(1818,1,False)])
  root=h.root
  def fail(action,*args):
   if action=='apply'and h.step in {0,2,4}:h.events.append((h.step,'apply'));raise h.m.AgentFailure('engine_start_failed')
   return root(action,*args)
  h.root=fail;h.run();self.assertEqual([x[0]for x in h.actions('apply')],[0,2,4,6]);self.assertFalse(h.actions('probe'))
 def test_source_fetch_retry_respects_777_and_clears_after_recovery(self):
  h=Harness(policy=self.policy(),steps=[(1000,1,False),(1310,1,False),(1316,1,False),(1318,1,False),(1348,1,False),(1350,1,False),(2126,1,False),(2128,1,False),(2131,1,False)])
  compile_source=h.compile
  def fail(*args):
   if h.step in {1,3,5}:h.events.append((h.step,'compile'));raise h.m.AgentFailure('source_fetch_denied')
   return compile_source(*args)
  h.compile=fail;h.run();self.assertEqual([x[0]for x in h.actions('compile')],[0,1,3,5,7]);self.assertFalse(h.actions('quarantine'));self.assertEqual(len(h.actions('apply')),1)
 def test_local_process_repair_obeys_custom_first_delay(self):
  h=Harness(policy=self.policy(),death_step=1,steps=[(1000,1,False),(1020,1,False),(1026,1,False),(1028,1,False)]);h.run();self.assertEqual([x[0]for x in h.actions('apply')],[0,3])
 def test_legacy_default_schedule_is_unchanged(self):
  m=load('legacy_schedule_fixture','xenoid-proxy-agent.py');count=0;delays=[]
  for _ in range(6):count,delay=m._retry_schedule(None,count);delays.append(delay)
  self.assertEqual(delays,[5,15,60,60,60,60])

class EngineApply(unittest.TestCase):
 def fixture(self,retry,outbound_fails=False,local_fails=False):
  e=load('fixture_engine_'+str(id(self))+str(retry),'xenoid-proxy-engine.py');events=[]
  meta={'path':'/never-used','sha256':'1'*64,'generation':1,'name':'a'};o={'candidate':meta,'appliedGeneration':None,'activeCandidate':None}
  def local(*a):
   events.append('local_health')
   if local_fails:raise e.Error('engine_health_failed')
  def outbound(*a):
   events.append('outbound')
   if outbound_fails:raise e.Error('proxy_outbound_unavailable')
  mocks={'sha':lambda*a:'1'*64,'candidate_config':lambda*a:({},((7893,7895),(7897,7899)),1053,9091,'secret',False,True),'quarantine':lambda*a:events.append('quarantine'),'prerequisites':lambda:None,'validate_binary':lambda*a:None,'topology':lambda*a:None,'policy':lambda*a:None,'dataplane':lambda*a:None,'configure':lambda*a:None,'launch':lambda*a:object(),'health':local,'activate':lambda*a:events.append('activate'),'outbound_ready':outbound,'retry_proxy_policy':lambda:retry,'stop':lambda*a:events.append('stop_pending'),'save_owner':lambda*a:None}
  with ExitStack()as stack:
   for k,v in mocks.items():stack.enter_context(patch.object(e,k,v))
   try:e.apply({},o)
   except e.Error as ex:return events,ex.code,o
  return events,None,o
 def test_retry_apply_retains_structural_candidate_when_remote_down(self):
  events,error,o=self.fixture(True,True);self.assertIsNone(error);self.assertNotIn('outbound',events);self.assertNotIn('stop_pending',events);self.assertEqual(o['phase'],'applied-pending')
 def test_legacy_apply_keeps_old_strict_gate(self):
  events,error,o=self.fixture(False,True);self.assertEqual(error,'proxy_outbound_unavailable');self.assertIn('stop_pending',events);self.assertEqual(events.count('quarantine'),2)
 def test_local_health_failure_remains_guarded(self):
  events,error,o=self.fixture(True,local_fails=True);self.assertEqual(error,'engine_health_failed');self.assertIn('stop_pending',events);self.assertNotIn('outbound',events)
class PolicyAndGuards(unittest.TestCase):
 def test_source_host_policy_mismatch_never_applies(self):
  h=Harness();h.source['failurePolicy']['retry_delays_seconds']=[7]
  with self.assertRaises(h.m.AgentFailure)as caught:h.run()
  self.assertEqual(caught.exception.code,'failure_policy_mismatch');self.assertFalse(h.actions('apply'));self.assertFalse(h.actions('probe'))
 def test_default_policy_absence_is_legacy_and_max_int32_delay_supported(self):
  m=load('policy_guard_fixture','xenoid-proxy-agent.py')
  with patch.object(m,'load_proxy_routing_policy',return_value={}):self.assertIsNone(m._retry_policy({'source':{}}))
  policy=copy.deepcopy(POLICY);policy['retry_delays_seconds']=[2147483647]
  with patch.object(m,'load_proxy_routing_policy',return_value={'failure_policy':policy}):self.assertEqual(m._retry_policy({'source':{'failurePolicy':policy}}),policy)
 def test_desired_diagnostic_flag_is_strict_boolean(self):
  m=load('desired_guard_fixture','xenoid-proxy-agent.py');h=Harness()
  value={'schemaVersion':2,'instanceId':'fixture','generation':1,'enabled':True,'checkId':1,'source':h.source,'diagnosticRequested':'yes'}
  channel=types.SimpleNamespace(call=lambda*a:value)
  with self.assertRaises(m.AgentFailure):m._desired(channel,{'instanceId':'fixture'},0)
  value['diagnosticRequested']=False;self.assertFalse(m._desired(channel,{'instanceId':'fixture'},0)['diagnosticRequested'])
 def test_host_policy_digest_is_validated_before_accepting_status(self):
  m=load('live_policy_fixture','xenoid-proxy-agent.py');manifest={'instanceId':'fixture','resourceTag':'tag','runtimeEpoch':'epoch','manifestDigest':'1'*64,'generation':0}
  value={'ok':True,**manifest,'generation':1,'phase':'active','structuralApplied':True,'dataPlaneVerified':False,'capabilities':{k:False for k in m.CAPABILITY_KEYS},'counters':{},'selectedNode':'','nodeCount':0,'failurePolicy':POLICY,'failurePolicyDigest':'0'*64}
  with patch.object(m,'_run_root',return_value=value):
   with self.assertRaises(m.AgentFailure):m._live_status(manifest,pathlib.Path('/unused'))
   value['failurePolicyDigest']=hashlib.sha256(m._canonical(POLICY)).hexdigest();self.assertTrue(m._live_status(manifest,pathlib.Path('/unused'))['structuralApplied'])
 def test_online_cannot_be_claimed_without_real_diagnostic_flag(self):
  m=load('online_guard_fixture','xenoid-proxy-agent.py');live={'phase':'active','structuralApplied':True}
  with self.assertRaises(m.AgentFailure):m._continuous_live(live,POLICY,upstream='online',verified=False)
class CompilerBoundary(unittest.TestCase):
 def test_failure_policy_metadata_never_reaches_compiler_protocol(self):
  m=load('compiler_policy_fixture','xenoid-proxy-agent.py');h=Harness();requests=[];engine=types.SimpleNamespace(spawn=lambda kind:('fixture-worker',100,101),stop_worker=lambda worker:None)
  response={'type':'result','ok':True,'config':{},'nodeNames':['node'],'sourceSha256':'1'*64};m._STOP=False
  with patch.object(m,'_engine',return_value=engine),patch.object(m,'_write_worker',side_effect=lambda fd,value,deadline:requests.append(copy.deepcopy(value))),patch.object(m,'_read_worker',return_value=response),patch.object(m.os,'close'):
   self.assertEqual(m._compile_source({},h.source,{}),response)
  self.assertEqual(len(requests),1);self.assertNotIn('failurePolicy',requests[0]);self.assertNotIn('failure_policy',requests[0]);self.assertIn('failurePolicy',h.source)

class EngineReadOnlyPolicy(unittest.TestCase):
 def test_failure_policy_command_has_no_lock_owner_or_mutator(self):
  e=load('policy_command_fixture','xenoid-proxy-engine.py');module=types.SimpleNamespace(load_proxy_routing_policy=lambda:{'failure_policy':POLICY},normalize_proxy_failure_policy=lambda p:p);output=io.StringIO();events=[]
  with patch.object(e.os,'geteuid',return_value=0),patch.object(e,'load_manifest',return_value={}),patch.object(e,'live',side_effect=lambda m:events.append('runtime_checked')),patch.object(e,'routing_module',return_value=module),patch.object(e,'mkdir',side_effect=AssertionError('no directory writes')),patch.object(e,'owner',side_effect=AssertionError('no owner needed')),patch.object(e.os,'open',side_effect=AssertionError('no lock/file creation')),redirect_stdout(output):
   self.assertEqual(e.main(['failure-policy','--manifest','/unused']),0)
  result=json.loads(output.getvalue());self.assertEqual(events,['runtime_checked']);self.assertEqual(result['failurePolicy'],POLICY)
 def test_missing_policy_readback_is_legacy_null(self):
  e=load('policy_missing_fixture','xenoid-proxy-engine.py');module=types.SimpleNamespace(load_proxy_routing_policy=lambda:{})
  with patch.object(e,'routing_module',return_value=module):self.assertEqual(e.failure_policy_status({}),{'ok':True,'failurePolicy':None,'failurePolicyDigest':''})
 def test_failure_policy_read_rejects_wrong_runtime(self):
  e=load('policy_wrong_owner_fixture','xenoid-proxy-engine.py');out=io.StringIO()
  with patch.object(e.os,'geteuid',return_value=0),patch.object(e,'load_manifest',return_value={}),patch.object(e,'live',side_effect=e.Error('runtime_identity_mismatch')),patch.object(e,'failure_policy_status',side_effect=AssertionError('must check runtime first')),redirect_stdout(out):self.assertEqual(e.main(['failure-policy','--manifest','/unused']),1)
  self.assertEqual(json.loads(out.getvalue())['error'],'runtime_identity_mismatch')
class EngineLiveness(unittest.TestCase):
 def test_local_liveness_has_one_local_request_and_no_netfilter_walk(self):
  e=load('liveness_fixture','xenoid-proxy-engine.py');commands=[];checks=[];path=types.SimpleNamespace(exists=lambda:True)
  m={'instanceId':'fixture','resourceTag':'tag','runtimeEpoch':'epoch','manifestDigest':'1'*64,'proxyNamespace':'owned','paths':{'config':'/unused'}};o={'phase':'applied','appliedGeneration':1,'activeCandidate':'a'}
  with patch.object(e,'validate_control'),patch.object(e,'validate_ipc'),patch.object(e,'proc_path',return_value=path),patch.object(e,'checked_process',side_effect=lambda*a:checks.append(a)),patch.object(e,'candidate_config',return_value=({},None,None,9091,'secret',False,True)),patch.object(e,'netns_command',return_value=['nsenter','--net=/owned','--']),patch.object(e,'tool',side_effect=lambda n:n),patch.object(e,'run',side_effect=lambda argv,**kw:commands.append(argv)or types.SimpleNamespace(returncode=0)):
   result=e.liveness(m,o)
  self.assertTrue(result['structuralApplied']);self.assertFalse(result['dataPlaneVerified']);self.assertEqual(len(commands),1);self.assertIn('http://127.0.0.1:9091/version',commands[0]);self.assertFalse(any('iptables'in str(a)or'sysctl'in str(a)for a in commands));self.assertEqual(len(checks),2)
 def test_missing_owned_process_differs_from_live_identity_mismatch(self):
  e=load('missing_process_fixture','xenoid-proxy-engine.py');m={'engine':{'binarySha256':'1'*64}};record={'pid':123,'generation':1,'candidate':'a','supervisor':'systemd-unit','binarySha256':'1'*64}
  with patch.object(e,'read_json',return_value=record),patch.object(e,'identity',side_effect=e.Error('process_identity_mismatch')),patch.object(e.Path,'exists',return_value=False):
   with self.assertRaises(e.Error)as caught:e.checked_process(m,pathlib.Path('/unused'),1,'a')
   self.assertEqual(caught.exception.code,'engine_process_failed')
  with patch.object(e,'read_json',return_value=record),patch.object(e,'identity',side_effect=e.Error('process_identity_mismatch')),patch.object(e.Path,'exists',return_value=True):
   with self.assertRaises(e.Error)as caught:e.checked_process(m,pathlib.Path('/unused'),1,'a')
   self.assertEqual(caught.exception.code,'process_identity_mismatch')

class UnverifiedDiagnosticAudit(unittest.TestCase):
 def fixture(self):
  m=load('unverified_diagnostic_fixture_'+str(id(self)),'xenoid-proxy-agent.py')
  manifest={'instanceId':'fixture','resourceTag':'tag','runtimeEpoch':'e'*64,'manifestDigest':'a'*64,'generation':1}
  counters={x:5 for x in ('ingress','proxyUplink','egress','ingressBytes','proxyUplinkBytes','egressBytes')}
  counters.update({cap[:-len('Proxy')]+stage:5 for cap in m.CAPABILITY_KEYS for stage in ('Ingress','Uplink','Egress')})
  before={**manifest,'phase':'active','structuralApplied':True,'dataPlaneVerified':False,'counters':counters,'capabilities':{k:False for k in m.CAPABILITY_KEYS},'failurePolicy':copy.deepcopy(POLICY),'failurePolicyDigest':hashlib.sha256(m._canonical(POLICY)).hexdigest()}
  desired={'generation':1,'checkId':2,'source':{'udpAllowed':False,'ipv6Allowed':True}}
  channel=types.SimpleNamespace(call=lambda*a:{'checkId':2,'capabilities':{k:'Udp'not in k for k in m.CAPABILITY_KEYS},'elapsedMs':1,'errorCode':''})
  return m,manifest,before,desired,channel
 def invoke(self,m,manifest,before,desired,channel,after):
  with patch.object(m,'_live_status',return_value=after)as live:
   result=m._continuous_diagnostic(channel,desired,manifest,pathlib.Path('/unused'),before,POLICY)
  return result,live.call_count
 def test_equal_complete_counters_keep_real_route_but_not_network_proof(self):
  m,manifest,before,desired,channel=self.fixture();(result,error),calls=self.invoke(m,manifest,before,desired,channel,copy.deepcopy(before))
  self.assertEqual(calls,2);self.assertEqual(error,'probe_path_unverified');self.assertEqual(result['upstreamState'],'unknown');self.assertFalse(result['dataPlaneVerified']);self.assertTrue(result['structuralApplied'])
 def test_partially_advancing_complete_set_is_still_truthfully_unverified(self):
  m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before);after['counters']['ingress']+=1
  (result,error),_=self.invoke(m,manifest,before,desired,channel,after);self.assertEqual(error,'probe_path_unverified');self.assertFalse(result['dataPlaneVerified'])
 def test_missing_or_decreasing_or_invalid_counters_remain_rejected(self):
  for kind in ('missing','decreasing','negative','boolean','extra'):
   with self.subTest(kind=kind):
    m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before)
    if kind=='missing':after['counters'].pop('v4DnsIngress')
    elif kind=='decreasing':after['counters']['v4DnsIngress']-=1
    elif kind=='negative':after['counters']['ingress']=-1
    elif kind=='boolean':after['counters']['ingress']=True
    else:after['counters']['foreign']=0
    with self.assertRaises(m.AgentFailure)as caught:self.invoke(m,manifest,before,desired,channel,after)
    self.assertEqual(caught.exception.code,'data_plane_unverified')
 def test_before_counter_set_is_also_required_complete(self):
  m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before);before['counters'].pop('ingressBytes')
  with self.assertRaises(m.AgentFailure):self.invoke(m,manifest,before,desired,channel,after)
 def test_epoch_generation_owner_and_manifest_changes_remain_rejected(self):
  for key in ('runtimeEpoch','generation','instanceId','resourceTag','manifestDigest'):
   with self.subTest(key=key):
    m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before);after[key]=2 if key=='generation'else'changed'
    with self.assertRaises(m.AgentFailure):self.invoke(m,manifest,before,desired,channel,after)
 def test_host_policy_change_during_diagnostic_is_not_accepted(self):
  m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before);after['failurePolicy']=dict(POLICY,retry_delays_seconds=[7]);after['failurePolicyDigest']=hashlib.sha256(m._canonical(after['failurePolicy'])).hexdigest()
  with self.assertRaises(m.AgentFailure)as caught:self.invoke(m,manifest,before,desired,channel,after)
  self.assertEqual(caught.exception.code,'failure_policy_mismatch')
 def test_fresh_structural_failure_is_never_reclassified_as_network_unknown(self):
  m,manifest,before,desired,channel=self.fixture()
  with patch.object(m,'_probe_data_plane',side_effect=m.AgentFailure('data_plane_unverified')),patch.object(m,'_live_status',side_effect=m.AgentFailure('process_identity_mismatch')):
   with self.assertRaises(m.AgentFailure)as caught:m._continuous_diagnostic(channel,desired,manifest,pathlib.Path('/unused'),before,POLICY)
  self.assertEqual(caught.exception.code,'process_identity_mismatch')
 def test_run_retains_route_on_manual_path_unverified_without_reapply(self):
  h=Harness(steps=[(1000,1,False),(1020,2,True),(1040,2,False)]);original=h.live
  def live(*args,**kw):
   value=original(*args,**kw);value.update(instanceId='fixture',resourceTag='tag',runtimeEpoch='fixture',manifestDigest='0'*64,failurePolicy=copy.deepcopy(POLICY),failurePolicyDigest=hashlib.sha256(h.m._canonical(POLICY)).hexdigest())
   value['counters']={key:5 for key in ('ingress','proxyUplink','egress','ingressBytes','proxyUplinkBytes','egressBytes')};value['counters'].update({cap[:-len('Proxy')]+stage:5 for cap in h.m.CAPABILITY_KEYS for stage in ('Ingress','Uplink','Egress')});return value
  def probe(*args,**kw):h.events.append((h.step,'probe'));raise h.m.AgentFailure('data_plane_unverified')
  h.live=live;h.probe=probe;h.run();self.assertEqual(len(h.actions('apply')),1);self.assertEqual(len(h.actions('probe')),1);self.assertFalse(h.actions('quarantine'));self.assertFalse(h.actions('rollback'))
  reports=[(r,e)for _,_,r,e in h.actions('report')if e=='probe_path_unverified'];self.assertEqual(len(reports),1);self.assertEqual(reports[0][0]['upstreamState'],'unknown');self.assertFalse(reports[0][0]['dataPlaneVerified'])
 def test_actual_fresh_audit_refuses_stopped_structure(self):
  m,manifest,before,desired,channel=self.fixture();after=copy.deepcopy(before);after['phase']='off'
  with self.assertRaises(m.AgentFailure):self.invoke(m,manifest,before,desired,channel,after)

if __name__=='__main__':unittest.main()
