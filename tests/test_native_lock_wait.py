"""Actual pinned native flock provider across real processes; no device calls."""
import contextlib,hashlib,importlib.util,os,subprocess,sys,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa'
sys.path.insert(0,str(QA))
import test_qa_native_lock as native_fixture
instance_operation_lock=native_fixture.NATIVE.instance_operation_lock
InstanceError=native_fixture.InstanceError
assert hashlib.sha256(native_fixture.NATIVE_SOURCE.encode()).hexdigest()=='9e469ec947782bbce398e23b6c9d249afafdab12f111be8a188970c01c404c3e'
def load(path,name):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
new=load(QA/'script_host.py','production_lock_wait')
CID='a'*64

class Manager:
 def __init__(self,state):
  self.context=types.SimpleNamespace(state_root=state,instance_id=state.name,resource_tag='fixture');self.cid=CID;self.held=[];self.tap=Mock(return_value={'ok':True})
 def instance_operation_lock(self,**options):self.held.append(options['timeout_seconds']);return instance_operation_lock(self.context.state_root,**options)
 def _owned_container_record(self,**options):return {'Id':self.cid,'State':{'Running':True}},None
 def daemon_client(self,**options):return types.SimpleNamespace(tap=self.tap)

class LockWait(unittest.TestCase):
 def setUp(self):
  self.native_error=patch.dict(sys.modules,{'xenoid.config':types.SimpleNamespace(InstanceError=InstanceError)});self.native_error.start();self.addCleanup(self.native_error.stop)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.manager=Manager(Path(self.tmp.name)/'fixture-instance')
 def holder(self,seconds):
  source="import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from test_qa_native_lock import NATIVE\nwith NATIVE.instance_operation_lock(Path(sys.argv[2])):\n print('LOCKED',flush=True);time.sleep(float(sys.argv[3]))\n"
  process=subprocess.Popen([sys.executable,'-B','-c',source,str(ROOT/'tests'),str(self.manager.context.state_root),str(seconds)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  self.assertEqual(process.stdout.readline().strip(),'LOCKED')
  def reap():
   try:process.communicate(timeout=seconds+3)
   except subprocess.TimeoutExpired:process.terminate();process.communicate(timeout=3)
   self.assertEqual(process.returncode,0)
  self.addCleanup(reap);return process
 def broker(self,module=new,seconds=0):return module.Broker(self.manager,Mock(),config={'expected_container_id':CID,'command_timeout':seconds})
 def test_panel_zero_waits_past_six_seconds_for_native_maintenance(self):
  self.holder(6.35);before=time.monotonic()
  with self.broker().owned_manager():self.assertGreaterEqual(time.monotonic()-before,6.2)
 def test_finite_panel_budget_and_explicit_override_are_honored_before_body(self):
  self.holder(1.5);before=time.monotonic()
  with self.assertRaises(new.TransportUnavailable):
   with self.broker(seconds=1).owned_manager():self.fail('body/input entered after timeout')
  self.assertTrue(.9<=time.monotonic()-before<1.4)
 def test_wait_is_stop_aware_without_weakening_EX(self):
  self.holder(.8);broker=self.broker();timer=threading.Timer(.1,broker.stopping.set);timer.start();self.addCleanup(timer.join)
  with self.assertRaisesRegex(RuntimeError,'stopping'):
   with broker.owned_manager():self.fail('cancelled input entered')
 def test_stop_during_successful_native_acquisition_slice_never_enters_body(self):
  self.holder(.18);broker=self.broker();timer=threading.Timer(.04,broker.stopping.set);timer.start();self.addCleanup(timer.join)
  with self.assertRaisesRegex(RuntimeError,'stopping'):
   with broker.owned_manager():self.fail('stopped body admitted after a successful acquisition')
 def test_existing_owned_forward_cleanup_is_allowed_after_stop_but_not_other_commands(self):
  broker=self.broker();broker.stopping.set();self.manager.adb_target='fixture-adb'
  result=types.SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
  with patch.object(new.shutil,'which',return_value='/fixture/adb'),patch.object(new.subprocess,'run',return_value=result)as run:
   self.assertTrue(broker.owned_call(['forward','--remove','tcp:123'],4)['ok']);self.assertEqual(run.call_count,1)
   with self.assertRaisesRegex(RuntimeError,'stopping'):broker.owned_call(['shell','input','keyevent','4'],4)
   self.assertEqual(run.call_count,1)
 def test_runtime_or_registered_identity_change_stays_fatal(self):
  broker=self.broker();self.manager.cid='b'*64
  with self.assertRaises(new.RuntimeIdentityError):
   with broker.owned_manager():self.fail('changed runtime admitted')
  self.manager.cid=CID;other=Manager(self.manager.context.state_root/'other');broker.verify=Mock(side_effect=[self.manager,other])
  with self.assertRaises(new.RuntimeIdentityError):
   with broker.owned_manager():self.fail('changed registry admitted')
 def test_old_navigation_offer_expires_during_real_wait_without_any_tap(self):
  self.holder(.2);broker=self.broker();broker._native_navigation_offer={'token':'offer','generation':0,'created_at':time.monotonic()-new.NATIVE_NAV_MAX_AGE_SECONDS+.05,'bounds':[0,0,10,10]}
  broker.ensure_native_interaction=Mock(return_value={'dump':'fixture','profile':{}})
  with patch.object(new,'native_touch_point',return_value=(1,2,None)):
   result=broker.handle_native_navigation('offer','back')
  self.assertFalse(result['handled']);self.assertEqual(result['reason'],'navigation_observation_stale');self.assertFalse(result['input_sent']);self.manager.tap.assert_not_called()
  self.assertEqual(broker.handle_native_navigation('offer','back')['reason'],'navigation_observation_not_current')
 def test_lost_input_acknowledgement_is_not_retried(self):
  broker=self.broker();broker.ensure_native_interaction=Mock(return_value={'dump':'fixture','profile':{}});self.manager.tap.side_effect=TimeoutError('lost ACK')
  with patch.object(new,'native_touch_point',return_value=(1,2,None)):
   with self.assertRaises(TimeoutError):broker.official_tap(1,2)
  self.assertEqual(self.manager.tap.call_count,1)
 def test_explicit_unlimited_override_is_not_capped_by_finite_panel_default(self):
  self.holder(1.2);before=time.monotonic()
  with self.broker(seconds=1).owned_manager(deadline=None):self.assertGreaterEqual(time.monotonic()-before,1.1)
 def test_final_reader_cleanup_waits_for_real_native_owner_after_stop(self):
  self.holder(1.2);before=time.monotonic();broker=self.broker(seconds=1)
  reader=Mock();reader.manager=self.manager;broker._ui_reader=reader;broker.stopping.set()
  broker.cleanup(drain=True)
  self.assertGreaterEqual(time.monotonic()-before,1.1)
  reader.close.assert_called_once_with(force=False);self.assertIsNone(broker._ui_reader)
  self.manager.tap.assert_not_called()
 def test_expired_command_budget_never_starts_subprocess(self):
  broker=self.broker()
  def executable(_):time.sleep(.04);return'/fixture/adb'
  with patch.object(new.shutil,'which',side_effect=executable),patch.object(new.subprocess,'run')as run:
   with self.assertRaisesRegex(new.TransportUnavailable,'before sending'):broker.owned_call(['version'],.02)
  run.assert_not_called()
 def test_invalid_lock_is_not_retried_as_contention(self):
  self.manager.context.state_root.mkdir();path=self.manager.context.state_root/'operation.lock';path.write_text('');other=path.with_suffix('.foreign');os.link(path,other)
  with self.assertRaisesRegex(InstanceError,'lock invalid'):
   with self.broker().owned_manager():self.fail('invalid lock admitted')

if __name__=='__main__':unittest.main()
