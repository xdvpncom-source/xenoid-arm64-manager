#!/usr/bin/env python3
"""Owned proxy routing and optional internet diagnostics have separate proofs."""
from copy import deepcopy
import json
from pathlib import Path
import shutil,subprocess,sys,tempfile,unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from xenoid.daemon_client import proxy_routing_ready,_validated_proxy_status,_failure_policy_digest
from xenoid.proxy_source import normalize_proxy_routing_policy,normalize_proxy_failure_policy,proxy_routing_rules,ProxyRoutingError
from xenoid.proxy_controller import ProxyController
from xenoid.operation_lock import instance_operation_lock, operation_lock_is_held
from xenoid import cli
from xenoid.live_observe import LiveAcceptance,_Checks,_Budget
POLICY={'schema':'dev.xenoid.proxy-failure-policy/v1','mode':'retry_proxy','retry_delays_seconds':[5,15,60]}
EPOCH='a'*64;INSTANCE='11111111-1111-4111-8111-111111111111'
COUNTERS={n:1 for n in ('ingress','proxyUplink','egress','ingressBytes','proxyUplinkBytes','egressBytes')}
COUNTERS.update({f'v{f}{c}{s}':1 for f in(4,6)for c in('Dns','Tcp','Udp')for s in('Ingress','Uplink','Egress')})
CAPS={f'v{f}{c}Proxy':False for f in(4,6)for c in('Dns','Tcp','Udp')}
def state(upstream='unknown'):
 report={'generation':7,'checkId':9,'phase':'active','structuralApplied':True,'dataPlaneVerified':False,
         'capabilities':deepcopy(CAPS),'counters':deepcopy(COUNTERS),'errorCode':'',
         'failurePolicy':'retry_proxy','failurePolicyDigest':_failure_policy_digest(POLICY),'upstreamState':upstream}
 if upstream=='retrying':report['errorCode']='probe_timeout'
 return {'ok':True,'schemaVersion':2,'stateReadable':True,'stateError':None,'instanceId':INSTANCE,
         'generation':7,'checkId':9,'enabled':True,'configured':True,'sourceKind':'endpoint','selectedNode':'',
         'udpAllowed':False,'allowInsecureHttp':False,'ipv6Allowed':True,'quarantined':False,'runtimeEpoch':EPOCH,
         'report':report,'probe':None,'failurePolicy':deepcopy(POLICY)}
def host(status):
 return {'ok':True,'phase':'active','structuralApplied':True,'generation':status['generation'],
         'runtimeEpoch':status['runtimeEpoch'],'counters':deepcopy(COUNTERS),'failurePolicy':deepcopy(POLICY),
         'failurePolicyDigest':_failure_policy_digest(POLICY)}
class Continuity(unittest.TestCase):
 def test_offline_and_unknown_have_structural_proof_without_fake_online(self):
  for upstream in('unknown','retrying'):
   s=state(upstream);self.assertTrue(proxy_routing_ready(s,host(s)));self.assertFalse(s['report']['dataPlaneVerified'])
 def test_unproven_manual_probe_keeps_only_current_structurally_valid_route(self):
  value=state();value['report']['errorCode']='probe_path_unverified'
  self.assertTrue(proxy_routing_ready(value,host(value)));self.assertFalse(value['report']['dataPlaneVerified'])
  for key,bad in [('upstreamState','retrying'),('dataPlaneVerified',True),('structuralApplied',False),('errorCode','data_plane_unverified')]:
   broken=deepcopy(value);broken['report'][key]=bad;self.assertFalse(proxy_routing_ready(broken,host(broken)))
  broken=host(value);broken['counters'].pop('ingress');self.assertFalse(proxy_routing_ready(value,broken))
  broken=host(value);broken['counters']['ingress']=0;self.assertFalse(proxy_routing_ready(value,broken))
 def test_route_proof_does_not_wait_for_inflight_manual_diagnostic(self):
  s=state();s['checkId']+=1;self.assertTrue(proxy_routing_ready(s,host(s)))
 def test_wrong_generation_epoch_policy_and_counter_reset_remain_rejected(self):
  s=state()
  for field,value in [('generation',8),('runtimeEpoch','b'*64),('failurePolicyDigest','c'*64),('structuralApplied',False)]:
   h=host(s);h[field]=value;self.assertFalse(proxy_routing_ready(s,h),field)
  h=host(s);h['counters']['ingress']=0;self.assertFalse(proxy_routing_ready(s,h))
  for key,value in [('failurePolicyDigest','c'*64),('generation',8),('phase','quarantined'),('errorCode','agent_channel_auth_failed')]:
   v=deepcopy(s);v['report'][key]=value;self.assertFalse(proxy_routing_ready(v,host(v)),key)
 def test_report_cannot_opt_in_when_daemon_owned_policy_is_absent(self):
  s=state();s.pop('failurePolicy');self.assertFalse(proxy_routing_ready(s,host(s)))
 def test_legacy_keeps_requested_internet_proof_requirement(self):
  s=state();s.pop('failurePolicy')
  for key in('failurePolicy','failurePolicyDigest','upstreamState'):s['report'].pop(key)
  self.assertFalse(proxy_routing_ready(s,host(s)))
 def test_optional_policy_schema_roundtrip_and_strict_rejection(self):
  s=state();self.assertEqual(_validated_proxy_status(s,INSTANCE),s)
  for bad in(None,{},True,{'mode':'direct'}):
   v=deepcopy(s);v['failurePolicy']=bad;self.assertFalse(_validated_proxy_status(v,INSTANCE)['ok'])
 def test_actual_agent_retained_lkg_fixture_keeps_readiness_for_only_four_codes(self):
  import runpy
  fixture=json.loads((ROOT/'daemon/app/src/test/resources/proxy-retained-lkg.json').read_text())
  agent=runpy.run_path(str(ROOT/'scripts/xenoid-proxy-agent.py'))
  class Channel:
   def call(self,action,value):self.action=action;self.value=value
  for item in fixture['reports']:
   value=state();live=host(value);live.update(dataPlaneVerified=False,selectedNode='node-001',nodeCount=1,nodes=['node-001'],capabilities=deepcopy(CAPS))
   c=Channel();agent['_report_status'](c,{'generation':7,'checkId':9},agent['_continuous_live'](live,POLICY),error_code=item['error'])
   self.assertEqual(c.value,item['report']);value['report']=c.value
   self.assertTrue(proxy_routing_ready(value,host(value)),item['error'])
   checks=_Checks();ids={};manager=SimpleNamespace(proxy_engine_status=lambda:host(value))
   LiveAcceptance(manager)._proxy_checks(manager,{}, {'proxyGeneration':7,'runtimeEpoch':EPOCH},ids,{'proxy':value,'bootstrap':{'components':{'proxy':{'ok':True}}}},_Budget(None),checks)
   self.assertIsNone(checks.first_failure());self.assertEqual(ids['proxyDiagnosticState'],'unknown');self.assertFalse(ids['proxyDataPlaneVerified'])
   for bad in('ownership_mismatch','routing_policy_invalid','source_invalid','agent_channel_auth_failed'):
    rejected=deepcopy(value);rejected['report']['errorCode']=bad;self.assertFalse(proxy_routing_ready(rejected,host(rejected)))
 def test_engine_policy_reply_is_exact_and_never_guesses_an_empty_success(self):
  from xenoid.backend import RuntimeManager
  manager=RuntimeManager.__new__(RuntimeManager)
  manager.context=SimpleNamespace(resource_tag='fixture')
  good={'ok':True,'failurePolicy':POLICY,'failurePolicyDigest':_failure_policy_digest(POLICY)}
  for value,expected in [(good,True),({'ok':True},False),({**good,'failurePolicyDigest':'a'*64},False),({'ok':True,'failurePolicy':None,'failurePolicyDigest':''},True)]:
   manager._proxy_root_process=lambda *args,**kwargs:(0,(json.dumps(value)+'\n').encode())
   self.assertEqual(manager._proxy_root_json('failure-policy')['ok'],expected)
 def test_manual_check_failure_has_no_host_mutation_or_recovery(self):
  class Manager:
   def ensure_instance_lease(self):pass
  m=Manager();m.context=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture');m.cfg=object();m.lease=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture')
  with tempfile.TemporaryDirectory()as tmp:
   m.context.state_root=Path(tmp)/INSTANCE;daemon=SimpleNamespace(proxy_status=lambda:state(),proxy_check=lambda:{'ok':True,'generation':7,'checkId':10});controller=ProxyController(m.context,m.cfg,m.lease,m,daemon)
   with patch('xenoid.proxy_controller.wait_for_proxy_check',return_value={'ok':False,'error':'probe_timeout'})as checked:
    self.assertEqual(controller.status(check=True),{'ok':False,'error':'probe_timeout'});checked.assert_called_once_with(daemon,expected_check_id=10,expected_generation=7,expected_runtime_epoch=EPOCH)
 def test_manual_diagnostic_releases_actual_native_lock_for_other_workers(self):
  class Manager:
   def ensure_instance_lease(self):pass
  with tempfile.TemporaryDirectory()as tmp:
   m=Manager();m.context=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture',state_root=Path(tmp)/INSTANCE);m.cfg=object();m.lease=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture')
   allocated=[]
   def allocate():
    self.assertTrue(operation_lock_is_held(INSTANCE));allocated.append(1)
    return {'ok':True,'generation':7,'checkId':10}
   client=SimpleNamespace(proxy_status=lambda:state(),proxy_check=allocate);controller=ProxyController(m.context,m.cfg,m.lease,m,client)
   entered=threading.Event();finish=threading.Event();results=[]
   def wait(_client,**ticket):
    self.assertFalse(operation_lock_is_held(INSTANCE));self.assertEqual(ticket,{'expected_check_id':10,'expected_generation':7,'expected_runtime_epoch':EPOCH})
    entered.set();self.assertTrue(finish.wait(3));return {'ok':False,'error':'probe_timeout'}
   with patch('xenoid.proxy_controller.wait_for_proxy_check',side_effect=wait):
    worker=threading.Thread(target=lambda:results.append(controller.status(check=True)));worker.start()
    try:
     self.assertTrue(entered.wait(3))
     with instance_operation_lock(m.context.state_root,timeout_seconds=0):self.assertTrue(worker.is_alive())
    finally:finish.set();worker.join(3)
   self.assertEqual(allocated,[1]);self.assertEqual(results,[{'ok':False,'error':'probe_timeout'}])
 def test_actual_cli_dispatch_releases_lock_but_retains_preparation_guards(self):
  import os
  class Manager:
   def ensure_instance_lease(self):pass
   def migrate_legacy_token_state(self):
    self.assert_locked();events.append('migrate')
   def assert_locked(self):
    if not operation_lock_is_held(INSTANCE):raise AssertionError('owner lock missing')
  with tempfile.TemporaryDirectory()as tmp:
   context=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture',state_root=Path(tmp)/INSTANCE)
   config=object();lease=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture');manager=Manager();manager.context=context;manager.cfg=config;manager.lease=lease
   args=SimpleNamespace(top_command='proxy',check=True,func=cli.cmd_proxy_status,instance='phone-6')
   events=[];allocated=[];results=[];entered=threading.Event();finish=threading.Event()
   def allocate():
    manager.assert_locked();allocated.append(1);return {'ok':True,'generation':7,'checkId':10}
   client=SimpleNamespace(proxy_status=lambda:state(),proxy_check=allocate)
   def pending(_args):manager.assert_locked();events.append('pending');return None
   def wait(_client,**ticket):
    self.assertFalse(operation_lock_is_held(INSTANCE));self.assertFalse(args._operation_lock_held)
    self.assertEqual(ticket,{'expected_check_id':10,'expected_generation':7,'expected_runtime_epoch':EPOCH})
    entered.set();self.assertTrue(finish.wait(3));return {'ok':False,'error':'probe_timeout'}
   with patch.dict(os.environ,{'XENOID_EXPECT_INSTANCE_ID':INSTANCE}),patch.object(cli,'build_parser',return_value=SimpleNamespace(parse_args=lambda _argv:args)),patch.object(cli,'resolve_project_root',return_value=Path(tmp)),patch.object(cli,'select_instance_name',return_value='phone-6'),patch.object(cli,'resolve_instance',return_value=(context,config,lease)),patch.object(cli,'runtime',return_value=manager),patch.object(cli,'daemon',return_value=client),patch.object(cli,'_reject_unrelated_regeneration_mutation',side_effect=pending),patch.object(cli,'wait_for_proxy_check',side_effect=wait),patch.object(cli,'_emit_proxy_result',return_value=1):
    worker=threading.Thread(target=lambda:results.append(cli.main([])));worker.start()
    try:
     self.assertTrue(entered.wait(3))
     with instance_operation_lock(context.state_root,timeout_seconds=0):self.assertTrue(worker.is_alive())
    finally:finish.set();worker.join(3)
   self.assertEqual(events,['pending','migrate']);self.assertEqual(allocated,[1]);self.assertEqual(results,[1])
 def test_cli_pending_regeneration_rejects_diagnostic_before_allocation(self):
  with tempfile.TemporaryDirectory()as tmp:
   context=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture',state_root=Path(tmp)/INSTANCE)
   args=SimpleNamespace(check=True,context=context,config=object(),lease=SimpleNamespace(instance_id=INSTANCE),project_root=Path(tmp),instance_name='phone-6')
   blocked={'ok':False,'error':'device_regeneration_pending'}
   with patch.object(cli,'_refresh_instance_under_lock'),patch.object(cli,'_reject_unrelated_regeneration_mutation',return_value=blocked),patch.object(cli,'runtime',side_effect=AssertionError('must not migrate or allocate')),patch.object(cli,'print_json')as printed:
    self.assertEqual(cli.cmd_proxy_status(args),1);printed.assert_called_once_with(blocked)
   self.assertFalse(args._operation_lock_held)
 def test_cli_legacy_diagnostic_keeps_native_serialization(self):
  class Manager:
   def migrate_legacy_token_state(self):self.migrated=True
  with tempfile.TemporaryDirectory()as tmp:
   context=SimpleNamespace(instance_id=INSTANCE,state_root=Path(tmp)/INSTANCE)
   args=SimpleNamespace(check=True,context=context);manager=Manager()
   legacy=state();legacy.pop('failurePolicy')
   def old_path(_args,callback):
    self.assertTrue(operation_lock_is_held(INSTANCE));return {'ok':True,'legacy':True}
   with patch.object(cli,'_refresh_instance_under_lock'),patch.object(cli,'_reject_unrelated_regeneration_mutation',return_value=None),patch.object(cli,'runtime',return_value=manager),patch.object(cli,'daemon',return_value=SimpleNamespace(proxy_status=lambda:legacy)),patch.object(cli,'_call_proxy_controller',side_effect=old_path),patch.object(cli,'wait_for_proxy_check',side_effect=AssertionError('legacy must use its original locked path')),patch.object(cli,'_emit_proxy_result',return_value=0):
    self.assertEqual(cli.cmd_proxy_status(args),0)
   self.assertTrue(manager.migrated);self.assertFalse(args._operation_lock_held)
 def test_policy_reimport_preserves_source_and_flags_with_owner_guard(self):
  current=state();current.pop('failurePolicy');current['report']={};current['quarantined']=True
  original={'kind':'endpoint','value':'socks5://proxy.example:1080','selectedNode':'node-001',
            'udpAllowed':False,'allowInsecureHttp':False,'ipv6Allowed':True}
  events=[];calls=[]
  class Manager:
   def ensure_instance_lease(self):pass
   def proxy_failure_policy(self):return {'ok':True,'failurePolicy':deepcopy(POLICY),'failurePolicyDigest':_failure_policy_digest(POLICY)}
   def proxy_prerequisite(self):return {'ok':True}
   def proxy_quarantine(self,*args):events.append('guard');return {'ok':True}
   def proxy_engine_status(self):return host(current)
  class Daemon:
   def proxy_status(self):return deepcopy(current)
   def proxy_export(self):return {'ok':True,'source':deepcopy(original)}
   def proxy_source(self,*args,**kwargs):
    events.append('source');calls.append((args,kwargs));current.clear();current.update(state());current['generation']=8;current['report']['generation']=8
    return {'ok':True,'generation':8}
  m=Manager();m.context=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture');m.cfg=object();m.lease=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture')
  with tempfile.TemporaryDirectory()as tmp:
   m.context.state_root=Path(tmp)/INSTANCE;c=ProxyController(m.context,m.cfg,m.lease,m,Daemon());c._ensure_agent=lambda status:{'ok':True}
   with patch('xenoid.proxy_controller.wait_for_proxy_check',side_effect=AssertionError('no automatic external check')):
    self.assertTrue(c.reconcile_desired()['ok'])
  self.assertEqual(events,['guard','source']);self.assertEqual(len(calls),1)
  args,kwargs=calls[0];self.assertEqual(args,('endpoint',original['value'],True))
  self.assertEqual(kwargs,{'selected_node':'node-001','udp_allowed':False,'allow_insecure_http':False,'ipv6_allowed':True,'failure_policy':POLICY})
 def test_live_acceptance_requires_route_and_records_offline_truthfully(self):
  for upstream in('unknown','retrying'):
   s=state(upstream);m=SimpleNamespace(proxy_engine_status=lambda:host(s));checks=_Checks();ids={}
   LiveAcceptance(m)._proxy_checks(m,{}, {'proxyGeneration':7,'runtimeEpoch':EPOCH},ids,
      {'proxy':s,'bootstrap':{'components':{'proxy':{'ok':True}}}},_Budget(None),checks)
   self.assertIsNone(checks.first_failure());self.assertTrue(checks.values['proxyRouting']['ok'])
   self.assertEqual(ids['proxyDiagnosticState'],upstream);self.assertFalse(ids['proxyDataPlaneVerified'])
 def test_policy_never_creates_extra_direct_rules(self):
  old={'schema':'dev.xenoid.qa.proxy-routing/v1','exceptions':[{'domain':'example.org','include_subdomains':True}]}
  new={**deepcopy(old),'failure_policy':deepcopy(POLICY)}
  self.assertEqual(proxy_routing_rules(old),proxy_routing_rules(new));self.assertEqual(proxy_routing_rules(new)[-1],'MATCH,GLOBAL')
  self.assertNotIn('failure_policy',normalize_proxy_routing_policy(old))
 def test_delay_validation_is_representation_bound_and_copies_input(self):
  p=deepcopy(POLICY);p['retry_delays_seconds']=[3601,86400,2147483647];self.assertEqual(normalize_proxy_failure_policy(p),p)
  for delays in([0],[True],[1.0],[-1],[2147483648],[],[1]*9):
   with self.assertRaises(ProxyRoutingError):normalize_proxy_failure_policy({**POLICY,'retry_delays_seconds':delays})
  normalized=normalize_proxy_failure_policy(POLICY);normalized['retry_delays_seconds'].append(120);self.assertEqual(POLICY['retry_delays_seconds'],[5,15,60])
 @unittest.skipUnless(shutil.which('javac')and shutil.which('java'),'JDK required')
 def test_real_java_policy_digest_readiness_and_immutability(self):
  source=ROOT/'daemon/app/src/main/java/dev/xenoid/daemon/ProxyFailurePolicy.java'
  body='''package dev.xenoid.daemon;
import java.util.*;
public final class PolicyContract {
 public static void main(String[] args) {
  Map<String,Object> p=new LinkedHashMap<>();p.put("schema",ProxyFailurePolicy.SCHEMA);p.put("mode","retry_proxy");p.put("retry_delays_seconds",Arrays.asList(5L,15L,60L));
  Map<String,Object> clean=ProxyFailurePolicy.normalize(p);
  if(!ProxyFailurePolicy.digest(clean).equals(args[0]))throw new AssertionError("cross-language digest");
  Map<String,Object> r=new LinkedHashMap<>();r.put("failurePolicy","retry_proxy");r.put("failurePolicyDigest",args[0]);r.put("phase","active");r.put("structuralApplied",true);r.put("generation",7L);r.put("dataPlaneVerified",false);r.put("upstreamState","unknown");r.put("errorCode","");
  if(!ProxyFailurePolicy.routingReady(clean,7L,r)||ProxyFailurePolicy.routingReady(null,7L,r))throw new AssertionError("policy authority");
  r.put("upstreamState","retrying");r.put("errorCode","probe_timeout");if(!ProxyFailurePolicy.routingReady(clean,7L,r))throw new AssertionError("offline routing");
  r.put("errorCode","agent_channel_auth_failed");if(ProxyFailurePolicy.routingReady(clean,7L,r))throw new AssertionError("security error");
  r.put("errorCode","probe_timeout");r.put("failurePolicyDigest","bad");if(ProxyFailurePolicy.routingReady(clean,7L,r))throw new AssertionError("wrong digest");
  try{((List)clean.get("retry_delays_seconds")).add(0);throw new AssertionError("mutable");}catch(UnsupportedOperationException expected){}
  for(Object bad:Arrays.asList(0,-1,true,1.0,2147483648L)){p.put("retry_delays_seconds",Arrays.asList(bad));try{ProxyFailurePolicy.normalize(p);throw new AssertionError("bad delay");}catch(IllegalArgumentException expected){}}
  p.put("retry_delays_seconds",Arrays.asList(2147483647L));ProxyFailurePolicy.normalize(p);
 }
}'''
  with tempfile.TemporaryDirectory()as folder:
   d=Path(folder);f=d/'PolicyContract.java';f.write_text(body)
   subprocess.run(['javac','-d',str(d),str(source),str(f)],capture_output=True,check=True)
   subprocess.run(['java','-cp',str(d),'dev.xenoid.daemon.PolicyContract',_failure_policy_digest(POLICY)],capture_output=True,check=True)
if __name__=='__main__':unittest.main()
