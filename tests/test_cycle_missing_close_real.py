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
    def __init__(self):self.taps=[];self.window=WINDOW;self.snapshot=None
    def settings(self):return {'android_user':0}
    def inspect_window(self,**_):
        window=copy.deepcopy(self.window);window.setdefault('display_id',0)
        window['focus_observation']={'schema':'xenoid.wm-focus-observation/v1','exact_match':window.get('stable')is True,
            'basis':'independent_wm_focus_pair','display_id':window['display_id'],'token':'abc123','user':0,
            'package':window.get('package'),'component':window.get('component'),'geometry_match':True,
            'geometry':[204,364,0],'elapsed_seconds':0,'hierarchy_state':'unknown_legacy_root'}
        window['native_navigation_observation']='xenoid-native-navigation/v1:local-fixture'
        self.snapshot=window;return window
    def click_window_action(self,window,action):
        assert window['observation']==self.snapshot['native_navigation_observation']and action=='back'
        back=m.Cycle.observed_navigation_back(self.snapshot);assert back is not None
        left,top,right,bottom=back['bounds'];self.tap((left+right)//2,(top+bottom)//2)
        return {'handled':True,'action':'back','official_backend':'xenoid_native','input_sent':True}
    def tap(self,x,y):self.taps.append((x,y))
    def log(self,_):pass
    def adb(self,_):raise AssertionError('No ADB input')
class Tests(unittest.TestCase):
    def service(self,c,window):
        c.device.window=window
        return c.service_missing_ad_close(c.observe())
    def test_actual_continue_cta_is_not_close_and_observed_back_is_selected(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');clock=Clock()
        self.assertTrue(m.is_ad_activity(WINDOW));self.assertIsNone(c.ad_close_control(WINDOW))
        with patch.object(m,'time',clock):
            self.assertFalse(self.service(c, WINDOW));clock.now=16
            self.assertTrue(self.service(c, WINDOW))
        self.assertEqual(d.taps,[(42,345)])
    def test_same_creative_new_accessibility_window_ids_cannot_repeat_back(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');clock=Clock()
        with patch.object(m,'time',clock):
            self.service(c, WINDOW);clock.now=16;self.service(c, WINDOW)
            changed=copy.deepcopy(WINDOW)
            for item in changed['controls']+changed['navigation_controls']:item['window_id']+=2000
            clock.now=60;self.assertFalse(self.service(c, changed))
            clock.now=80;self.assertFalse(self.service(c, changed))
        self.assertEqual(d.taps,[(42,345)])
if __name__=='__main__':unittest.main()
