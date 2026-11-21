import copy,importlib.util,json,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
BASE=Path(__file__).parent
sys.modules['xenoid_device']=types.SimpleNamespace(Device=object)
spec=importlib.util.spec_from_file_location('missing_ad',BASE.parent/'qa/vpn-cycle.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def fixture(name):return json.loads((BASE/'fixtures/ad_windows'/name).read_text())['window']
AD={**fixture('actual-admob-webview-no-controls.json'),'display_id':0,'navigation_controls':fixture('actual-all-windows-ad.json')['navigation_controls']}
MAIN={'package':'v2.vpn.fast','component':'v2.vpn.fast/com.xdvpn.app.MainActivity','stable':True,'controls':[]}
class Clock:
    now=0
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds
class Device:
    def __init__(self,after=MAIN):self.window=AD;self.after=after;self.taps=[]
    def settings(self):return {'android_user':0}
    def inspect_window(self,**_):return self.window
    def tap(self,x,y):self.taps.append((x,y));self.window=self.after
    def log(self,_):pass
    def adb(self,_):raise AssertionError('No ADB input or additional probe')
class Tests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.time=patch.object(m,'time',self.clock);self.time.start();self.addCleanup(self.time.stop)
    def test_actual_unexposed_ad_uses_observed_back_after_grace_and_verifies_main(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast')
        with patch.object(m,'AD_READY_SECONDS',35):c.dismiss_ads()
        self.assertEqual(d.taps,[(42,345)]);self.assertGreaterEqual(self.clock.now,18)
    def test_unchanged_ad_fails_after_one_back(self):
        d=Device(after=AD);c=m.Cycle(d,'v2.vpn.fast')
        with patch.object(m,'AD_READY_SECONDS',35):
            with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertEqual(d.taps,[(42,345)])
    def test_no_generic_main_or_permission_back(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast')
        for window in [MAIN,{**AD,'component':'v2.vpn.fast/com.xdvpn.app.MainActivity'},
                       {**AD,'package':'com.android.permissioncontroller','component':'com.android.permissioncontroller/.GrantPermissionsActivity'}]:
            self.assertFalse(c.service_missing_ad_close(window));self.clock.now+=20
            self.assertFalse(c.service_missing_ad_close(window))
        self.assertFalse(d.taps)
    def test_no_back_before_fifteen_seconds_or_without_current_navigation(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');self.assertFalse(c.service_missing_ad_close(AD));self.clock.now=14.9
        self.assertFalse(c.service_missing_ad_close(AD));self.clock.now=16
        self.assertFalse(c.service_missing_ad_close({**AD,'navigation_controls':[]}));self.assertFalse(d.taps)
    def test_ambiguous_close_controls_are_not_classified_as_missing(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');w=copy.deepcopy(fixture('actual-all-windows-ad.json'))
        close=next(x for x in w['controls']if x['text']=='Close');other={**close,'bounds':[10,10,25,25]};w['controls'].append(other)
        self.assertIsNone(c.ad_close_control(w));self.assertFalse(c.service_missing_ad_close(w));self.clock.now=30
        self.assertFalse(c.service_missing_ad_close(w));self.assertFalse(d.taps)
    def test_configured_zero_disables_fallback(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast');c.service_missing_ad_close(AD);self.clock.now=30
        with patch.object(m,'AD_MISSING_CLOSE_BACK_LIMIT',0):self.assertFalse(c.service_missing_ad_close(AD))
        self.assertFalse(d.taps)
    def test_loading_progress_and_ephemeral_ids_do_not_restart_ad_grace(self):
        d=Device();c=m.Cycle(d,'v2.vpn.fast')
        def loading(percent,window_id):
            w=copy.deepcopy(AD);w['controls'].append({'class_name':'android.widget.ProgressBar','resource_id':'',
                'text':'Refreshing server list %s%%'%percent,'content_desc':'','bounds':[1,1,percent,5],
                'enabled':True,'clickable':False,'window_id':window_id})
            return w
        self.assertTrue(m.is_loading(loading(10,1),'v2.vpn.fast'))
        self.assertFalse(c.service_missing_ad_close(loading(10,1)));self.clock.now=8
        self.assertFalse(c.service_missing_ad_close(loading(40,2)));self.clock.now=16
        self.assertTrue(c.service_missing_ad_close(loading(80,3)))
        self.assertEqual(d.taps,[(42,345)])
    def test_loading_actual_ad_is_closed_in_both_ad_and_prepare_phases(self):
        for phase in ('dismiss','prepare','vpn'):
            self.clock.now=0;w=copy.deepcopy(AD);w['controls'].append({'text':'Refreshing server list',
                'content_desc':'','bounds':[1,1,100,20],'enabled':True,'clickable':False,'class_name':'android.view.View'})
            d=Device();d.window=w;c=m.Cycle(d,'v2.vpn.fast')
            with patch.object(m,'AD_READY_SECONDS',35):
                if phase=='dismiss':c.dismiss_ads()
                elif phase=='prepare':c.prepare_screen(35)
                else:
                    with patch.object(m,'VPN_READY_SECONDS',35),patch.object(c,'vpn_allowed',return_value=True):c.approve_vpn()
            self.assertEqual(d.taps,[(42,345)])
    def test_unchanged_state_cannot_repeat_but_new_creative_has_separate_budget(self):
        d=Device(after=AD);c=m.Cycle(d,'v2.vpn.fast')
        self.assertFalse(c.service_missing_ad_close(AD));self.clock.now=16
        self.assertTrue(c.service_missing_ad_close(AD));self.clock.now=40
        self.assertFalse(c.service_missing_ad_close(AD))
        second=copy.deepcopy(AD);second['controls'].append({'text':'Campaign B','content_desc':'',
            'bounds':[1,1,100,20],'enabled':True,'clickable':False,'class_name':'android.view.View'})
        self.assertFalse(c.service_missing_ad_close(second));self.clock.now=56
        self.assertTrue(c.service_missing_ad_close(second));self.assertEqual(len(d.taps),2)
if __name__=='__main__':unittest.main()
