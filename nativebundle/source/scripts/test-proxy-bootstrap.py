#!/usr/bin/env python3
"""Real journal/Executor and backend-hook regressions; no Android or network."""
import contextlib,copy,fnmatch,hashlib,importlib.util,json,os,shutil,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('XENOID_TEST_BASE_PROJECT',str(ROOT))).resolve()
sys.path.insert(0,str(BASE/'src'))
import xenoid
xenoid.__path__.insert(0,str(ROOT/'src/xenoid'))
from xenoid import backend as b,convergence as c,daemon_client as dc,proxy_bootstrap as p,proxy_controller as pc
spec=importlib.util.spec_from_file_location('stock_convergence_fixture',BASE/'scripts/test-convergence.py')
stock=importlib.util.module_from_spec(spec);sys.modules[spec.name]=stock;spec.loader.exec_module(stock)
POLICY={'schema':'dev.xenoid.proxy-failure-policy/v1','mode':'retry_proxy','retry_delays_seconds':[5,15,60]}
TEXT='socks5://qa-user:qa-fixture-password@8.8.8.8:1\n'


class Daemon:
 def __init__(self,manager,request):self.manager=manager;self.request=request;self.source=None;self.enabled=False;self.generation=0
 def proxy_status(self):
  return {'ok':True,'stateReadable':True,'instanceId':stock.INSTANCE_ID,'generation':self.generation,
   'enabled':self.enabled,'configured':self.source is not None,'failurePolicy':self.request['failurePolicy']if self.source else None,'runtimeEpoch':'test-runtime-epoch-12345678'}
 def proxy_export(self):return {'ok':True,'instanceId':stock.INSTANCE_ID,'generation':self.generation,'enabled':self.enabled,'source':copy.deepcopy(self.source)}
 def bootstrap_status(self):return {'runtimeEpoch':'test-runtime-epoch-12345678'}


class Controller:
 def __init__(self,context,cfg,lease,manager,daemon):self.m=manager;self.d=daemon
 def _mutation_scope(self):return contextlib.nullcontext()
 def _quarantine_for_mutation(self):self.m.events.append('DROP');return {'ok':True}
 def set_source(self,kind,value,enabled,**flags):
  self.m.events.append('authenticated-set-source');self.d.generation+=1;self.d.enabled=enabled
  self.d.source={'kind':kind,'value':value,'selectedNode':flags['selected_node'],'udpAllowed':flags['udp_allowed'],
   'ipv6Allowed':flags['ipv6_allowed'],'allowInsecureHttp':flags['allow_insecure_http'],'failurePolicy':copy.deepcopy(POLICY)}
  self.m.snapshot['components']['proxy'].update(state='matching',generation=self.d.generation,enabled=enabled)
  if self.m.crash_after_commit:self.m.crash_after_commit=False;raise RuntimeError('simulated-crash-after-authenticated-commit')
  return self.reconcile_desired()
 def reconcile_desired(self):
  self.m.events.append('reconcile-proxy')
  if self.d.source is None:raise AssertionError('unconfigured state reached DIRECT release')
  if self.d.enabled:return {'ok':True,'generation':self.d.generation,'enabled':True,'routingReady':True,'dataPlaneVerified':False}
  self.m.events.append('explicit-OFF')
  return {'ok':True,'generation':self.d.generation,'enabled':False,'phase':'off','structuralApplied':True,
   'dataPlaneVerified':True,'agentAbsent':True,'routingAbsent':True}


class Acceptance(stock.FakeAcceptance):
 def observe(self,context,expected,mode,deadline,progress):
  result=super().observe(context,expected,mode,deadline,progress)
  if mode=='convergence-resolve':
   for key,value in expected['componentActions'].items():
    if value is not None and value!='inspect':result['componentActions'][key]=copy.deepcopy(value)
   result['componentActions']['proxy']='reuse' # Pending bootstrap must override reuse.
  return result


@contextlib.contextmanager
def fixture(*,enabled=True,selected_node=''):
 with tempfile.TemporaryDirectory(dir=Path.cwd(),prefix='proxy-bootstrap-unit-')as directory:
  source=Path(directory)/'source.conf';source.write_text(TEXT);source.chmod(0o600)
  request=p.make_request(stock.INSTANCE_ID,source,kind='endpoint',udp_allowed=False,ipv6_allowed=True,
   allow_insecure_http=False,failure_policy=POLICY,enabled=enabled,selected_node=selected_node)
  snapshot=stock.healthy_snapshot();snapshot['runtime'].update(state='absent',containerId=None,dataUuid=stock.TARGET_DATA_UUID,
   rootfsUuid=stock.TARGET_ROOTFS_UUID,bootSeedRequired=True)
  snapshot['components']['proxy'].update(state='unknown',generation=None,enabled=None)
  with stock.manager_fixture(snapshot)as manager:
   manager.events=[];manager.crash_after_commit=False;manager.context.resource_tag='aaaaaaaaaaaa';manager.cfg=types.SimpleNamespace()
   manager.lease=types.SimpleNamespace(network_name='test-network',volume_name='test-volume');manager._pending_proxy_restore=None
   daemon=Daemon(manager,request);manager.daemon_client=lambda:daemon
   manager.proxy_failure_policy=lambda:{'ok':True,'failurePolicy':copy.deepcopy(POLICY)}
   manager.proxy_quarantine=lambda:(manager.events.append('DROP')or{'ok':True})
   manager._proxy_failure=lambda code:{'ok':False,'code':code,'error':code}
   manager._container_has_lease_owner=lambda owned:True
   manager._network_matches_lease=lambda owned:True;manager._volume_matches_lease=lambda owned:True
   manager._inspect_docker_object=lambda kind,name,**kw:({'Id':name},None)
   manager._owned_container_record=lambda**kw:({'Id':manager.snapshot['runtime']['containerId'],
    'State':{'Running':manager.snapshot['runtime']['state']=='running'},'Config':{'Labels':{
    b._RUNTIME_INPUT_LABEL:stock.INPUT_DIGEST,b._RUNTIME_BOOT_INPUT_LABEL:stock.BOOT_DIGEST}}},'')
   for name in ('_proxy_bootstrap_binding','_proxy_bootstrap_export_matches','recover_proxy_bootstrap_checkpoint',
                '_reconcile_initial_proxy_bootstrap','_restore_proxy_desired_after_update','_discard_pending_proxy_restore','reconcile_proxy_desired'):
    setattr(manager,name,types.MethodType(getattr(b.RuntimeManager,name),manager))
   storage={'state':'committed','source':'fresh','instanceId':stock.INSTANCE_ID,'resourceTag':manager.context.resource_tag,
            'filesystemUuid':stock.TARGET_DATA_UUID}
   with patch.object(b,'StorageStateStore',lambda*args:types.SimpleNamespace(load=lambda:copy.deepcopy(storage))),\
        patch.object(pc,'ProxyController',Controller),patch.object(dc,'proxy_routing_ready',lambda result:result.get('routingReady')is True):
    executor=lambda:c.ConvergenceExecutor(manager,artifact_builder=stock.FakeArtifacts(manager),live_acceptance=Acceptance(manager))
    yield manager,daemon,request,source,executor,storage


class BootstrapIntegration(unittest.TestCase):
 def test_authenticated_import_precedes_any_unconfigured_off_and_offline_is_ready(self):
  with fixture()as(m,d,request,source,executor,storage):
   result=executor().run(proxy_bootstrap=request)
   self.assertTrue(result['ok'],result);self.assertIn('authenticated-set-source',m.events)
   self.assertLess(m.events.index('authenticated-set-source'),m.events.index('reconcile-proxy'))
   self.assertNotIn('explicit-OFF',m.events);self.assertEqual(d.generation,1);self.assertTrue(d.enabled)
   self.assertIsNone(c.ConvergenceJournal(m).load())
 def test_crash_after_real_desired_commit_resumes_same_journal_without_reimport(self):
  with fixture()as(m,d,request,source,executor,storage):
   m.crash_after_commit=True;result=executor().run(proxy_bootstrap=request)
   self.assertFalse(result['ok']);journal=c.ConvergenceJournal(m);state=journal.load();operation=state['operationId']
   self.assertEqual(state['proxyBootstrap']['phase'],'pending');self.assertEqual(d.generation,1)
   result=executor().run() # No CLI flag: the retained intent owns this resume.
   self.assertTrue(result['ok'],result);self.assertEqual(m.events.count('authenticated-set-source'),1)
   self.assertNotIn('explicit-OFF',m.events);self.assertIsNone(journal.load());self.assertEqual(len(operation),32)
 def test_changed_source_after_commit_stays_drop_and_does_not_retarget_journal(self):
  with fixture()as(m,d,request,source,executor,storage):
   m.crash_after_commit=True;self.assertFalse(executor().run(proxy_bootstrap=request)['ok'])
   journal=c.ConvergenceJournal(m);before=journal.path.read_bytes();source.write_text(TEXT+'changed')
   result=executor().run();self.assertFalse(result['ok']);self.assertEqual(journal.path.read_bytes(),before)
   self.assertEqual(m.events.count('authenticated-set-source'),1);self.assertNotIn('explicit-OFF',m.events)
   self.assertNotIn('qa-fixture-password',json.dumps(result));self.assertNotIn(str(source),json.dumps(result))
 def test_missing_source_before_first_import_never_calls_direct(self):
  with fixture()as(m,d,request,source,executor,storage):
   source.unlink();result=executor().run(proxy_bootstrap=request)
   self.assertFalse(result['ok']);self.assertNotIn('authenticated-set-source',m.events);self.assertNotIn('explicit-OFF',m.events)
 def test_wrong_data_binding_rejects_without_source_publication(self):
  with fixture()as(m,d,request,source,executor,storage):
   storage['filesystemUuid']='90000000-0000-4000-8000-000000000001';result=executor().run(proxy_bootstrap=request)
   self.assertFalse(result['ok']);self.assertNotIn('authenticated-set-source',m.events);self.assertNotIn('explicit-OFF',m.events)
 def test_completed_initial_intent_does_not_reenable_later_admin_off(self):
  with fixture()as(m,d,request,source,executor,storage):
   self.assertTrue(executor().run(proxy_bootstrap=request)['ok']);d.enabled=False;d.generation+=1
   m.snapshot['components']['proxy'].update(enabled=False,generation=d.generation,state='matching')
   result=executor().run();self.assertTrue(result['ok'],result);self.assertFalse(d.enabled)
   self.assertEqual(m.events.count('authenticated-set-source'),1)
 def test_explicit_off_is_distinct_from_automatic_fallback_and_preserves_node_flags(self):
  with fixture(enabled=False,selected_node='saved-node')as(m,d,request,source,executor,storage):
   result=executor().run(proxy_bootstrap=request);self.assertTrue(result['ok'],result)
   self.assertFalse(d.enabled);self.assertEqual(d.source['selectedNode'],'saved-node');self.assertFalse(d.source['udpAllowed'])
   self.assertLess(m.events.index('authenticated-set-source'),m.events.index('explicit-OFF'))


class IntentBoundary(unittest.TestCase):
 def test_host_bootstrap_files_do_not_change_image_keys_or_compiled_artifact_closure(self):
  from xenoid.artifacts import TARGETS
  from xenoid.runtime_image import RuntimeImageBuilder,_CONTEXT_INPUT_PATHS,compute_input_record
  changed=('src/xenoid/backend.py','src/xenoid/cli.py','src/xenoid/convergence.py','src/xenoid/proxy_bootstrap.py','scripts/test-proxy-bootstrap.py')
  self.assertFalse([(name,path,pattern)for name,target in TARGETS.items()for path in changed for pattern in target.sources if fnmatch.fnmatchcase(path,pattern)])
  extra=('profiles/runtime-v1/scripts/make-runtime-context.sh','profiles/runtime-v1/scripts/patch-runtime-props.py','src/xenoid/cold_profile.py','scripts/seed-cold-profile.py','scripts/seed-profile-boot-id.py')
  with tempfile.TemporaryDirectory()as directory:
   root=Path(directory)
   for relative in (*_CONTEXT_INPUT_PATHS,*extra,'runtime/redroid/fresh-bluejay-render.py'):
    source=ROOT/relative if(ROOT/relative).exists()else BASE/relative
    target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
   owner=object.__new__(RuntimeImageBuilder);owner.project_root=root
   owner._cold_profile=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_bytes())
   def keys():
    record=compute_input_record(base_image_id='sha256:'+'1'*64,
     artifact_tuples=[{'target':'daemon','inputSha256':'2'*64,'toolSha256':'3'*64,'outputs':[{'path':'daemon/app.apk','mode':'0644','size':1,'sha256':'4'*64}]}],
     context_inputs=owner._context_inputs(),google_inputs={},tool_inputs={},builder_inputs={},
     daemon_seed_contract={'packageName':'dev.example.daemon','sharedUserId':None,'systemDestination':'/system/priv-app/XenoidDaemon/XenoidDaemon.apk','signingLineage':['5'*64],'privilegedPermissions':[],'apkSha256':'4'*64,'versionCode':1})
    return record['inputSha256'],record['bootInputSha256']
   before=keys()
   for relative in changed:
    target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('# host-only source change\n')
   self.assertEqual(keys(),before)
   target=root/'runtime/redroid/fresh-bluejay-render.py';target.write_bytes(target.read_bytes()+b'\n# image input positive control\n')
   after=keys();self.assertNotEqual(after[0],before[0]);self.assertNotEqual(after[1],before[1])

 def test_source_permissions_links_and_public_redaction(self):
  with fixture()as(m,d,request,source,executor,storage):
   intent=p.bind_request(request,stock.TARGET_DATA_UUID);self.assertEqual(p.read_source(intent),TEXT)
   public=json.dumps(p.public_intent(intent));self.assertNotIn(str(source),public);self.assertNotIn('qa-fixture-password',public)
   source.chmod(0o644)
   with self.assertRaisesRegex(p.ProxyBootstrapError,'source_unsafe'):p.read_source(intent)
   source.unlink();source.symlink_to('/etc/hosts')
   with self.assertRaises(p.ProxyBootstrapError):p.read_source(intent)
 def test_unknown_or_malformed_metadata_rejects_without_source_text(self):
  with fixture()as(m,d,request,source,executor,storage):
   for key,value in [('enabled',1),('kind',[]),('sourceSha256','bad'),('selectedNode','x'*129)]:
    bad=dict(request);bad[key]=value
    with self.subTest(key=key),self.assertRaises(p.ProxyBootstrapError):p.validate_request(bad)
   with self.assertRaises(p.ProxyBootstrapError):p.validate_request(dict(request,value='qa-fixture-password'))


if __name__=='__main__':unittest.main()
