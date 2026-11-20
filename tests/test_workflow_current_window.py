"""A historical ANR must not drive the editable workflow's live crash probe."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from test_vpn_cycle import Clock,FakeDevice,control,window

spec=importlib.util.spec_from_file_location('current_window_workflow',Path(__file__).resolve().parents[1]/'qa/vpn-cycle.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class CurrentWindowWorkflowTests(unittest.TestCase):
 def setUp(self):
  self.clock=Clock();self.device=FakeDevice(self.clock);self.cycle=m.Cycle(self.device,'qa.vpn')
 def probe(self,live):
  archive='WINDOW MANAGER LAST ANR\n  mCurrentFocus=Window{111 u0 Application Not Responding: system}\n'
  self.device.adb=Mock(side_effect=lambda args:live if args[1].startswith('dumpsys window displays |') else archive+live)
  return patch.object(m,'time',self.clock)
 def test_archived_anr_does_not_trigger_expensive_ui_observation_for_live_app(self):
  self.cycle.observe=Mock(side_effect=AssertionError('ordinary live app needs no crash UI dump'))
  with self.probe('mCurrentFocus=Window{222 u0 qa.vpn/.MainActivity}\n'):
   self.assertFalse(self.cycle.service_crash_once())
  self.cycle.observe.assert_not_called()
 def test_actual_live_anr_still_uses_existing_close_control(self):
  self.cycle.observe=Mock(return_value=window('Application Not Responding: system',[control('Close app','android:id/aerr_close')],package='android'))
  self.cycle.tap=Mock(return_value=True)
  with self.probe('mCurrentFocus=Window{333 u0 Application Not Responding: system}\n'):
   self.assertTrue(self.cycle.service_crash_once())
  self.cycle.tap.assert_called_once()
  self.assertEqual(self.cycle.tap.call_args.args[0]['resource_id'],'android:id/aerr_close')

if __name__=='__main__':unittest.main()
