"""The editable workflow requests durable data-reset only on the regeneration draw."""
import copy,unittest
from unittest.mock import patch
from test_cycle_google_cache import CacheDevice,m
from test_vpn_cycle import Clock
class GoogleDevice(CacheDevice):
 def __init__(self,clock):super().__init__(clock);self.google_plans=[];self.unverified_once=False
 def regenerate(self,*,timeout,operation_key,post_cache=None,post_google=None):
  self.google_plans.append((operation_key,copy.deepcopy(post_google)))
  answer=super().regenerate(timeout=timeout,operation_key=operation_key,post_cache=post_cache)
  if post_google:
   proof={'ok':True,'data_cleared':True,'packages':post_google['clear_packages'],'registration_enabled':True,'checkin_requested':True,'network_registered':None}
   if self.unverified_once:self.unverified_once=False;proof['registration_enabled']=False
   answer['google_reset']=proof
  return answer
class Tests(unittest.TestCase):
 def setUp(self):
  self.clock=Clock();self.device=GoogleDevice(self.clock);self.c=m.Cycle(self.device,'qa.vpn')
  self.time=patch.object(m,'time',self.clock);self.time.start();self.addCleanup(self.time.stop)
 def test_draw5_requests_exact_packages_and_two_second_stage_delays(self):
  with patch.object(m.random,'randint',return_value=5):self.assertTrue(self.c.regenerate_after_success())
  self.assertEqual(self.device.google_plans[0][1],{'clear_packages':['com.google.android.gms','com.google.android.gsf'],'user':'current','delay_seconds':2,'timeout':0})
  self.assertEqual(len(self.device.native_rotation_keys),1)
  self.assertTrue(any('microG device registration enabled'in x[2]for x in self.device.trace if x[0]=='log'))
 def test_other_draw_does_not_clear_or_register(self):
  with patch.object(m.random,'randint',return_value=4):self.assertFalse(self.c.regenerate_after_success())
  self.assertFalse(self.device.google_plans)
 def test_unverified_registration_retries_same_operation_without_new_draw(self):
  self.device.unverified_once=True
  with patch.object(m.random,'randint',return_value=5)as draw:self.c.regenerate_after_success()
  self.assertEqual(draw.call_count,1);self.assertEqual(len(self.device.google_plans),2)
  self.assertEqual(self.device.google_plans[0],self.device.google_plans[1]);self.assertEqual(len(self.device.native_rotation_keys),1)
 def test_script_can_disable_new_reset_and_keep_legacy_cache_plan(self):
  with patch.object(m,'GOOGLE_DATA_RESET_ENABLED',False),patch.object(m.random,'randint',return_value=5):self.c.regenerate_after_success()
  self.assertIsNone(self.device.google_plans[0][1]);self.assertEqual(len([x for x in self.device.trace if x[0]=='cache']),2)
 def test_script_timing_is_not_hardcoded_in_backend_request(self):
  with patch.object(m,'GOOGLE_RESET_DELAY_SECONDS',1),patch.object(m,'GOOGLE_RESET_TIMEOUT',77),patch.object(m.random,'randint',return_value=5):self.c.regenerate_after_success()
  self.assertEqual(self.device.google_plans[0][1]['delay_seconds'],1);self.assertEqual(self.device.google_plans[0][1]['timeout'],77)
if __name__=='__main__':unittest.main()
