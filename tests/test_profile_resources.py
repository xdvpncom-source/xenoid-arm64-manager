"""Generated display settings survive loops/timers and remain runtime-bound."""
import contextlib, copy, hashlib, importlib.util, json, sys, tempfile, types, unittest
from pathlib import Path
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa'))
import qa_resource_runtime as r
from test_qa_resources import AndroidDisplay
UUID='12345678-1234-1234-1234-123456789abc';TAG='0123456789ab';CID='c'*64

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
def write(p,v):p.write_text(json.dumps(v));p.chmod(0o600)

class AppliedProfileResourceTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
  self.policy=self.root/'policy.json';self.profiles=self.root/'profiles';self.profiles.mkdir();self.state=self.root/'resources';self.state.mkdir()
  self.context=types.SimpleNamespace(instance_id=UUID,resource_tag=TAG);self.manager=types.SimpleNamespace(context=self.context)
  self.owned={'Id':CID,'State':{'Running':True,'StartedAt':'same-docker-boot'}};self.config=r.validate_config({})
  write(self.policy,{'schema':'dev.xenoid.profile-policy/v1','enabled':True})
  for k,v in [('PROFILE_POLICY',self.policy),('PROFILE_STATE',self.profiles),('STATE',self.state)]:
   h=patch.object(r,k,v);h.start();self.addCleanup(h.stop)
  self.profile={'schema':'dev.xenoid.fingerprint/v1','display':{'width':224,'height':398,'densityDpi':80,'defaultRefreshRateHz':30,'peakRefreshRateHz':60}}
  self.profile['input']={'x':{'minimum':0,'maximum':223},'y':{'minimum':0,'maximum':397}}
  self.applied={'schema':'dev.xenoid.applied-profile/v1','instance_id':UUID,'resource_tag':TAG,'container_id':CID,'profile':self.profile,'profile_sha256':digest(self.profile),'policy_revision':7,'policy_sha256':'a'*64,'applied_at':'2026-11-21T21:00:00Z'}
  write(self.profiles/(UUID+'.json'),self.applied)
 def effective(self):return r.effective_config(self.config,self.manager,self.owned)
 def test_generated_display_overrides_central_size_dpi_and_fps(self):
  x=self.effective();self.assertEqual(x['fps'],30)
  before={'physical_size':(720,1280),'physical_density':560}
  self.assertEqual(r.desired_metrics(before,x),((224,398),80,False));self.assertEqual(r.desired_metrics(before,self.config),((200,356),72,False));self.assertEqual(self.config,r.DEFAULT_CONFIG)
 def test_generated_size_applies_even_when_scaling_null(self):
  self.config['scale']=None;self.assertEqual(r.desired_metrics({},self.effective()),((224,398),80,False))
 def test_disabled_or_missing_policy_preserves_old_behavior(self):
  write(self.policy,{'schema':'dev.xenoid.profile-policy/v1','enabled':False});self.assertEqual(self.effective(),self.config)
  self.assertEqual(self.effective()['fps'],20)
  self.policy.unlink();self.assertEqual(self.effective(),self.config)
 def test_missing_state_is_normal_for_new_device(self):
  (self.profiles/(UUID+'.json')).unlink();self.assertEqual(self.effective(),self.config)
 def test_copied_state_cannot_control_recreated_runtime(self):
  for k,v in [('container_id','d'*64),('instance_id','another-instance'),('resource_tag','another-tag')]:
   write(self.profiles/(UUID+'.json'),{**self.applied,k:v});self.assertEqual(self.effective(),self.config)
 def test_corrupt_matching_profile_fails_before_writes(self):
  x=copy.deepcopy(self.applied);x['profile']['display']['width']=225;write(self.profiles/(UUID+'.json'),x)
  with self.assertRaisesRegex(ValueError,'checksum'):self.effective()
 def test_invalid_profile_refresh_rate_fails_before_writes(self):
  for fps,peak in [(0,60),(30,20),(True,60),(29.5,60),(30,float('inf'))]:
   x=copy.deepcopy(self.applied);x['profile']['display'].update(defaultRefreshRateHz=fps,peakRefreshRateHz=peak);x['profile_sha256']=digest(x['profile']);write(self.profiles/(UUID+'.json'),x)
   with self.assertRaises(ValueError):self.effective()
 def test_mismatched_input_bounds_fail_before_applying_saved_dimensions(self):
  x=copy.deepcopy(self.applied);x['profile']['input']['x']['maximum']=224
  x['profile_sha256']=digest(x['profile']);write(self.profiles/(UUID+'.json'),x)
  with self.assertRaisesRegex(ValueError,'bounds disagree'):self.effective()
 def test_untrusted_private_file_or_invalid_policy_fails(self):
  self.policy.chmod(0o644)
  with self.assertRaisesRegex(ValueError,'Unsafe'):self.effective()
  write(self.policy,{'schema':'wrong','enabled':True})
  with self.assertRaisesRegex(ValueError,'policy'):self.effective()
 def test_policy_symlink_not_followed(self):
  saved=self.root/'saved.json';self.policy.rename(saved);self.policy.symlink_to(saved)
  with self.assertRaisesRegex(ValueError,'Unsafe'):self.effective()
 def test_new_profile_invalidates_marker_in_same_container_boot(self):
  old=r.resource_key(self.owned,self.effective(),{'instance_id':UUID,'resource_tag':TAG});x=copy.deepcopy(self.applied)
  x['profile']['display']['width']=226;x['profile']['input']['x']['maximum']=225;x['profile_sha256']=digest(x['profile']);write(self.profiles/(UUID+'.json'),x)
  new=r.resource_key(self.owned,self.effective(),{'instance_id':UUID,'resource_tag':TAG});self.assertNotEqual(old,new);self.assertEqual(old['container_id'],new['container_id'])
 def test_internal_dimensions_not_accepted_as_public_setting(self):
  with self.assertRaisesRegex(ValueError,'Unknown'):r.validate_config({'explicit_size':[400,800]})
 def pending(self,**changes):
  value={'schema':'dev.xenoid.pending-profile/v1','instance_id':UUID,'resource_tag':TAG,'container_id':CID,'operation_key':'cycle-8','target_sha256':'b'*64,'created_at':'now',**changes}
  write(self.profiles/(UUID+'.pending.json'),value)
 def test_pending_transaction_defers_timer_without_any_display_mutation(self):
  self.pending();b,a,_=self.broker()
  with patch.object(r,'ensure_host_vm_guard') as vm:
   result=r.reconcile_owned('phone-6',self.config,self.manager,force=True,broker=b)
  self.assertTrue(result['deferred']);self.assertEqual(result['skipped'],'profile_pending')
  vm.assert_not_called();b.set_frame_rate.assert_not_called();self.assertEqual(a.writes,[])
 def test_pending_transaction_stops_sdk_from_claiming_readiness(self):
  self.pending();b,a,_=self.broker()
  with patch.object(r,'load_config',return_value=self.config),self.assertRaisesRegex(RuntimeError,'still pending'):
   r.apply_for_broker(b)
  b.set_frame_rate.assert_not_called();self.assertEqual(a.writes,[])
 def test_pending_other_runtime_does_not_block_current_runtime(self):
  self.pending(container_id='e'*64);self.assertFalse(r.pending_profile(self.manager,self.owned))
 def test_invalid_pending_marker_fails_before_resource_changes(self):
  self.pending(target_sha256='invalid');b,a,_=self.broker()
  with self.assertRaisesRegex(ValueError,'pending'):
   r.reconcile_owned('phone-6',self.config,self.manager,force=True,broker=b)
  b.set_frame_rate.assert_not_called();self.assertEqual(a.writes,[])
 def broker(self):
  locks=[]
  @contextlib.contextmanager
  def lock(**kwargs):locks.append(kwargs);yield
  self.manager.instance_operation_lock=lock;self.manager._owned_container_record=lambda **kwargs:(self.owned,None)
  a=AndroidDisplay();b=types.SimpleNamespace(manager=self.manager,verify=None,owned_manager=lambda:contextlib.nullcontext(self.manager),owned_call=a.owned_call,set_frame_rate=Mock(return_value={'changed':False,'proxy':{'ready':True}}),regeneration_receipts=types.SimpleNamespace(folder=Path('/var/lib/xenoid-panel-scripts/6')),log=Mock())
  return b,a,locks
 def test_sdk_restores_generated_size_after_userspace_reload(self):
  b,a,locks=self.broker()
  with patch.object(r,'load_config',return_value=self.config),patch.object(r,'ensure_host_vm_guard',return_value={'ok':True}):
   self.assertTrue(r.apply_for_broker(b,timeout=0)['ok']);self.assertEqual((a.size,a.density),((224,398),80))
   a.size=(1440,3120);a.density=560
   self.assertTrue(r.apply_for_broker(b,timeout=0)['ok']);self.assertEqual((a.size,a.density),((224,398),80))
  self.assertEqual(locks,[{'timeout_seconds':None},{'timeout_seconds':None}]);b.set_frame_rate.assert_called_with(30,timeout=0)
  self.assertIn('generated profile',b.log.call_args.args[1]);self.assertTrue((self.state/'phone-6-original-display.json').exists())
 def test_disabled_sdk_leaves_runtime_alone(self):
  b=Mock()
  with patch.object(r,'load_config',return_value={**self.config,'enabled':False}):self.assertEqual(r.apply_for_broker(b),{'ok':True,'skipped':'disabled'})
  b.owned_manager.assert_not_called();b.set_frame_rate.assert_not_called()

class WireTests(unittest.TestCase):
 def test_sdk_wire_preserves_panel_budget_by_default(self):
  s=importlib.util.spec_from_file_location('resource_sdk',ROOT/'qa/script_runtime.py');sdk=importlib.util.module_from_spec(s);s.loader.exec_module(sdk)
  with patch.object(sdk,'request',side_effect=lambda *args:args):
   self.assertEqual(sdk.Device().apply_resource_defaults(),('apply-resource-defaults',[]));self.assertEqual(sdk.Device().apply_resource_defaults(timeout=0),('apply-resource-defaults',[0]));self.assertEqual(sdk.cli('device',['apply-resource-defaults','45']),('apply-resource-defaults',[45]))
 def test_new_scenario_inherits_and_explicit_legacy_override_remains(self):
  s=importlib.util.spec_from_file_location('resource_cycle',ROOT/'qa/vpn-cycle.py');m=importlib.util.module_from_spec(s)
  with patch.dict(sys.modules,{'xenoid_device':types.SimpleNamespace(Device=object)}):s.loader.exec_module(m)
  device=Mock();device.apply_resource_defaults.return_value={'ok':True}
  with patch.object(m,'configure_display')as display:
   m.configure_resources(device)
   device.apply_resource_defaults.assert_called_once_with();device.set_frame_rate.assert_not_called();display.assert_not_called()
  device.reset_mock()
  with patch.object(m,'DISPLAY_FPS',30),patch.object(m,'DISPLAY_SCALE','minimum'),patch.object(m,'configure_display')as display:
   m.configure_resources(device)
   device.apply_resource_defaults.assert_not_called();device.set_frame_rate.assert_called_once_with(30,timeout=m.DISPLAY_SETUP_TIMEOUT);display.assert_called_once_with(device,'minimum')
if __name__=='__main__':unittest.main()
