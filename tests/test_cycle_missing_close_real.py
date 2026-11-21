"""Replay actual all-window capture whose top-right button was a Continue CTA."""
import copy,importlib.util,json,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
BASE=Path(__file__).parent
sys.modules['xenoid_device']=types.SimpleNamespace(Device=object)
spec=importlib.util.spec_from_file_location('actual_missing_close',BASE.parent/'qa/vpn-cycle.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
WINDOW=json.loads((BASE/'fixtures/ad_windows/actual-unlabeled-continue-ad.json').read_text())['window']
class Clock:
    now=0
    def monotonic(self):return self.now
class Device:
    def __init__(self):self.taps=[]
    def settings(self):return {'android_user':0}
    def tap(self,x,y):self.taps.append((x,y))
    def log(self,_):pass
    def adb(self,_):raise AssertionError('No ADB input')
class Tests(unittest.TestCase):
    def test_actual_continue_cta_is_not_close_and_observed_back_is_selected(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');clock=Clock()
        self.assertTrue(m.is_ad_activity(WINDOW));self.assertIsNone(c.ad_close_control(WINDOW))
        with patch.object(m,'time',clock):
            self.assertFalse(c.service_missing_ad_close(WINDOW));clock.now=16
            self.assertTrue(c.service_missing_ad_close(WINDOW))
        self.assertEqual(d.taps,[(42,345)])
    def test_same_creative_new_accessibility_window_ids_cannot_repeat_back(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');clock=Clock()
        with patch.object(m,'time',clock):
            c.service_missing_ad_close(WINDOW);clock.now=16;c.service_missing_ad_close(WINDOW)
            changed=copy.deepcopy(WINDOW)
            for item in changed['controls']+changed['navigation_controls']:item['window_id']+=2000
            clock.now=60;self.assertFalse(c.service_missing_ad_close(changed))
            clock.now=80;self.assertFalse(c.service_missing_ad_close(changed))
        self.assertEqual(d.taps,[(42,345)])
if __name__=='__main__':unittest.main()
