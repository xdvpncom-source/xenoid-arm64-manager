"""The actual editable workflow shares one Connect-readiness wait budget."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock,patch
from test_vpn_cycle import Clock,FakeDevice,control,window
sys.modules.setdefault('xenoid_device',types.SimpleNamespace(Device=object))
spec=importlib.util.spec_from_file_location('connect_budget_cycle',Path(__file__).resolve().parents[1]/'qa/vpn-cycle.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Tests(unittest.TestCase):
 def setUp(self):
  self.clock=Clock();self.device=FakeDevice(self.clock);self.device.state='main';self.cycle=m.Cycle(self.device,'qa.vpn')
  for name,value in [('time',self.clock),('CONNECT_READY_SECONDS',10),('POLL_SECONDS',2),('POST_ACTION_QUIET_SECONDS',0),('CONNECT_POSITION',None)]:
   p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
 def taps(self):return [r for r in self.device.trace if r[0]=='tap']
 def test_partial_initial_snapshot_waits_for_later_real_connect(self):
  self.device.inspect_window=lambda **kw:window('MainActivity',[control('Connect')])
  self.cycle.connect(window('MainActivity',[]));self.assertEqual(len(self.taps()),1);self.assertEqual(self.taps()[0][1],2)
 def test_duplicate_initial_controls_wait_for_unique_real_control(self):
  self.device.inspect_window=lambda **kw:window('MainActivity',[control('Connect',box=(60,70,100,110))])
  self.cycle.connect(window('MainActivity',[control('Connect'),control('Connect',box=(40,40,60,60))]));self.assertEqual(len(self.taps()),1);self.assertEqual(self.taps()[0][-2:],(80,90))
 def test_each_preparation_receives_only_remaining_total_budget(self):
  budgets=[]
  def prepare(seconds):
   budgets.append(seconds);self.clock.sleep(3);return window('MainActivity',[])
  self.cycle.prepare_screen=prepare
  with self.assertRaisesRegex(RuntimeError,'No unique Connect control'):self.cycle.connect(window('MainActivity',[]))
  self.assertEqual(budgets,[8,3]);self.assertEqual(self.clock.now,10);self.assertFalse(self.taps())
 def test_deadline_expired_after_poll_never_becomes_unlimited_prepare_zero(self):
  self.cycle.prepare_screen=Mock(side_effect=AssertionError('no budget remains'))
  with patch.object(m,'CONNECT_READY_SECONDS',1),self.assertRaisesRegex(RuntimeError,'No unique Connect control'):
   self.cycle.connect(window('MainActivity',[]))
  self.cycle.prepare_screen.assert_not_called();self.assertEqual(self.clock.now,1);self.assertFalse(self.taps())
 def test_late_preparation_result_cannot_tap_after_total_deadline(self):
  def prepare(seconds):self.clock.sleep(seconds);return window('MainActivity',[control('Connect')])
  self.cycle.prepare_screen=prepare
  with self.assertRaisesRegex(RuntimeError,'No unique Connect control'):self.cycle.connect(window('MainActivity',[]))
  self.assertEqual(self.clock.now,10);self.assertFalse(self.taps())
 def test_zero_wait_is_unlimited_and_keeps_polling_real_snapshots(self):
  reads=[]
  def observe(**kw):
   reads.append(self.clock.now);return window('MainActivity',[control('Connect')] if len(reads)==4 else [])
  self.device.inspect_window=observe
  with patch.object(m,'CONNECT_READY_SECONDS',0):self.cycle.connect(window('MainActivity',[]))
  self.assertEqual(reads,[2,4,6,8]);self.assertEqual(len(self.taps()),1)
 def test_valid_initial_snapshot_taps_once_without_reinspection_or_delay(self):
  self.cycle.prepare_screen=Mock(side_effect=AssertionError('initial control already observed'))
  self.cycle.connect(window('MainActivity',[control('Connect',box=(10,20,50,60))]));self.assertEqual(self.taps(),[('tap',0,'main',30,40)]);self.cycle.prepare_screen.assert_not_called()
 def test_explicit_position_retains_immediate_fallback_behavior(self):
  self.cycle.prepare_screen=Mock(side_effect=AssertionError('configured point remains immediate'))
  with patch.object(m,'CONNECT_POSITION',(71,82)):self.cycle.connect(window('MainActivity',[]))
  self.assertEqual(self.taps(),[('tap',0,'main',71,82)]);self.cycle.prepare_screen.assert_not_called()
 def test_observed_control_still_wins_over_configured_position(self):
  with patch.object(m,'CONNECT_POSITION',(71,82)):self.cycle.connect(window('MainActivity',[control('Connect',box=(10,20,50,60))]))
  self.assertEqual(self.taps(),[('tap',0,'main',30,40)])
 def test_invalid_explicit_position_still_fails_without_touch(self):
  with patch.object(m,'CONNECT_POSITION',('71',82)),self.assertRaisesRegex(ValueError,'CONNECT_POSITION'):
   self.cycle.connect(window('MainActivity',[]))
  self.assertFalse(self.taps())
 def test_existing_preparation_handles_notification_then_connect(self):
  self.device.state='notification';self.cycle.connect(window('MainActivity',[]));self.assertEqual([r[2] for r in self.taps()],['notification','main'])
 def test_preparation_target_crash_is_not_hidden_by_connect_retries(self):
  self.cycle.prepare_screen=Mock(side_effect=RuntimeError('target crash'))
  with self.assertRaisesRegex(RuntimeError,'target crash'):self.cycle.connect(window('MainActivity',[]))
  self.cycle.prepare_screen.assert_called_once();self.assertFalse(self.taps())
 def test_no_snapshot_starts_preparation_with_full_budget(self):
  self.cycle.prepare_screen=Mock(return_value=window('MainActivity',[control('Connect')]))
  self.cycle.connect();self.cycle.prepare_screen.assert_called_once_with(10);self.assertEqual(len(self.taps()),1)
 def test_timeout_with_no_control_never_uses_blind_center_or_changes_app_data(self):
  self.device.inspect_window=lambda **kw:window('MainActivity',[])
  with self.assertRaisesRegex(RuntimeError,'No unique Connect control'):self.cycle.connect(window('MainActivity',[]))
  self.assertEqual(self.clock.now,10);self.assertFalse(self.taps());self.assertFalse(any(r[0] in {'open','clear','close'} for r in self.device.trace))
if __name__=='__main__':unittest.main()
