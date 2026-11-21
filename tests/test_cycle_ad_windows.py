import importlib.util
import copy
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

BASE = Path(__file__).parent
sys.modules['xenoid_device'] = types.SimpleNamespace(Device=object)
spec = importlib.util.spec_from_file_location('ad_fix', BASE.parent/'qa/vpn-cycle.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def fixture(name):
    return json.loads((BASE/'fixtures/ad_windows'/name).read_text())['window']

def control(desc='Close', box=(178,0,200,20), clickable=True):
    return {'content_desc':desc,'text':'','resource_id':'','class_name':'android.widget.ImageButton','bounds':list(box),'enabled':True,'clickable':clickable,'package':'v2.vpn.fast'}

def ad(item=None):
    root=control('Advertisement',(0,0,200,334),False);root['class_name']='android.widget.FrameLayout'
    return {'package':'v2.vpn.fast','component':'v2.vpn.fast/com.google.android.gms.ads.AdActivity','stable':True,'controls':[root,item or control()]}

MAIN={'package':'v2.vpn.fast','component':'v2.vpn.fast/com.xdvpn.app.MainActivity','stable':True,'controls':[control('Connect',(51,83,149,181))]}
W_AD=fixture('actual-all-windows-ad.json')
# Replay combines two real captures to model the next observed store window.
STORE={**fixture('actual-play-store-after-close.json'), 'display_id':0,
       'navigation_controls':W_AD['navigation_controls']}

class Clock:
    def __init__(self):self.now=0
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds

class Replay:
    def __init__(self,window,on_tap=None,on_back=MAIN,on_ad_back=None):
        self.window=window;self.on_tap=on_tap;self.on_back=on_back;self.on_ad_back=on_ad_back;self.actions=[];self.logs=[]
    def settings(self):return {'android_user':0}
    def inspect_window(self,**kwargs):self.actions.append(('read',));return self.window
    def tap(self,x,y):
        self.actions.append(('native_tap',x,y))
        if self.window.get('package')=='com.android.vending' and (x,y)==(42,345):self.window=self.on_back
        elif m.is_ad(self.window) and (x,y)==(42,345):
            if self.on_ad_back is not None:self.window=self.on_ad_back
        elif self.on_tap:self.window=self.on_tap
    def app_open(self):raise AssertionError('No app relaunch is allowed in landing recovery')
    def log(self,message):self.logs.append(message)
    def adb(self,args):raise AssertionError('Unexpected ADB command: '+repr(args))
    def back(self):raise AssertionError('ADB Back must never be used')

class AdTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.time=patch.object(m,'time',self.clock);self.time.start();self.addCleanup(self.time.stop)
        self.budget=patch.object(m,'AD_READY_SECONDS',16);self.budget.start();self.addCleanup(self.budget.stop)
    def cycle(self,device):return m.Cycle(device,'v2.vpn.fast')
    def test_actual_admob_inaccessible_webview_is_not_guessed(self):
        d=Replay(fixture('actual-admob-webview-no-controls.json'));c=self.cycle(d)
        self.assertTrue(m.is_ad(d.window));self.assertIsNone(c.ad_close_control(d.window))
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertFalse(any(a[0]=='native_tap'for a in d.actions))
    def test_actual_semantic_interstitial_close_is_preserved(self):
        d=Replay(fixture('actual-interstitial-close-button.json'));c=self.cycle(d)
        item=c.ad_close_control(d.window)
        self.assertEqual(item['content_desc'],'Interstitial close button')
        self.assertEqual(m.center(m.bounds(item)),(184,15))
    def test_real_all_windows_dump_exposes_close_missing_from_normal_dump(self):
        d=Replay(W_AD);c=self.cycle(d);item=c.ad_close_control(W_AD)
        self.assertEqual(item['text'],'Close');self.assertEqual(item['bounds'],[183,4,196,17])
        self.assertEqual(m.center(m.bounds(c.observed_navigation_back(W_AD))),(42,345))
    def test_anonymous_corner_not_automatically_called_close(self):
        d=Replay(ad(control('')));c=self.cycle(d)
        self.assertIsNone(c.ad_close_control(d.window))
        with patch.object(m,'AD_CLOSE_SELECTORS',({'class_name':'android.widget.ImageButton'},)):
            self.assertIsNotNone(c.ad_close_control(d.window))
    def test_external_store_after_verified_ad_close_returns_then_rechecks(self):
        d=Replay(ad(),on_tap=STORE);c=self.cycle(d);c.dismiss_ads()
        self.assertEqual([a[0]for a in d.actions].count('native_tap'),2)
        open_index=d.actions.index(('native_tap',42,345))
        self.assertGreaterEqual(sum(a[0]=='read'for a in d.actions[open_index+1:]),2)
        self.assertFalse(any('QA cycle completed successfully'in line for line in d.logs))
    def test_post_permission_idle_wait_also_recovers_proven_ad_landing(self):
        d=Replay(ad(),on_tap=STORE);c=self.cycle(d)
        def focus(args):
            self.assertEqual(args,m.CRASH_FOCUS_COMMAND)
            return 'mCurrentFocus=Window{1 u0 '+d.window['component']+'}'
        d.adb=focus
        c.wait(12,dialogs=True,ads=True,vpn=True)
        self.assertEqual([a for a in d.actions if a[0]=='native_tap'],
                         [('native_tap',189,10),('native_tap',42,345)])
        self.assertEqual(d.window,MAIN)
    def test_store_without_prior_ad_close_is_not_recovered(self):
        d=Replay(STORE);c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertFalse(any(a[0]=='native_tap'for a in d.actions))
    def test_unconfigured_foreign_package_is_not_recovered(self):
        browser={**STORE,'package':'unrelated.browser'};d=Replay(ad(),on_tap=browser);c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertNotIn(('native_tap',42,345),d.actions)
    def test_same_redirecting_target_is_not_clicked_again(self):
        w=ad(control(''));d=Replay(w,on_tap=STORE,on_back=w);c=self.cycle(d)
        with patch.object(m,'GEOMETRIC_AD_CLOSE',True):
            with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertEqual(sum(a[0]=='native_tap'for a in d.actions),2)
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_reopening_to_an_ad_is_not_success(self):
        d=Replay(ad(),on_tap=STORE,on_back=fixture('actual-admob-webview-no-controls.json'));c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_store_back_then_same_real_ad_uses_one_second_observed_back(self):
        d=Replay(W_AD,on_tap=STORE,on_back=W_AD,on_ad_back=MAIN);c=self.cycle(d)
        c.dismiss_ads()
        self.assertEqual([a for a in d.actions if a[0]=='native_tap'],
                         [('native_tap',189,10),('native_tap',42,345),('native_tap',42,345)])
        last_back=max(i for i,a in enumerate(d.actions)if a==('native_tap',42,345))
        self.assertGreaterEqual(sum(a[0]=='read'for a in d.actions[last_back+1:]),2)
        self.assertEqual(d.window,MAIN)
    def test_unchanged_origin_after_second_back_still_fails_without_more_input(self):
        d=Replay(W_AD,on_tap=STORE,on_back=W_AD);c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertEqual([a for a in d.actions if a[0]=='native_tap'],
                         [('native_tap',189,10),('native_tap',42,345),('native_tap',42,345)])
        self.assertFalse(any('QA cycle completed successfully'in line for line in d.logs))
    def test_second_back_requires_same_origin_component_and_live_provenance(self):
        d=Replay(W_AD,on_tap=STORE,on_back=W_AD);c=self.cycle(d)
        c.close_ad_control(W_AD,c.ad_close_control(W_AD));self.clock.now+=3
        self.assertTrue(c.return_from_ad_landing(STORE));self.clock.now+=3
        different={**W_AD,'component':'v2.vpn.fast/another.AdActivity'}
        self.assertFalse(c.return_from_ad_landing(different))
        self.clock.now+=m.AD_LANDING_PROVENANCE_SECONDS+1
        self.assertFalse(c.return_from_ad_landing(W_AD))
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_second_back_requires_fresh_real_navigation_control(self):
        d=Replay(W_AD,on_tap=STORE,on_back=W_AD);c=self.cycle(d)
        c.close_ad_control(W_AD,c.ad_close_control(W_AD));self.clock.now+=3
        self.assertTrue(c.return_from_ad_landing(STORE));self.clock.now+=3
        for window in ({**W_AD,'navigation_controls':[]},{**W_AD,'stable':False}):
            self.assertFalse(c.return_from_ad_landing(window))
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_no_generic_ad_back_without_preceding_store_return(self):
        d=Replay(W_AD);c=self.cycle(d)
        self.assertFalse(c.return_from_ad_landing(W_AD))
        self.assertFalse(d.actions)
    def test_return_to_main_with_inline_banner_does_not_send_second_back(self):
        inline={**W_AD,'component':MAIN['component']}
        self.assertTrue(m.is_ad(inline));self.assertFalse(m.is_ad_activity(inline))
        d=Replay(inline,on_tap=STORE,on_back=inline);c=self.cycle(d)
        c.close_ad_control(inline,c.ad_close_control(inline));self.clock.now+=3
        self.assertTrue(c.return_from_ad_landing(STORE));self.clock.now+=3
        self.assertFalse(c.return_from_ad_landing(inline))
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_same_unresponsive_semantic_target_is_bounded(self):
        d=Replay(ad());c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
        self.assertEqual(sum(a[0]=='native_tap'for a in d.actions),2)
    def test_new_accessibility_window_ids_do_not_reset_unchanged_target_budget(self):
        first=copy.deepcopy(W_AD);d=Replay(first);c=self.cycle(d)
        for offset in (0,10):
            current=copy.deepcopy(first)
            for control in current['controls']:control['window_id']+=offset
            selected=c.ad_close_control(current);self.assertIsNotNone(selected)
            c.close_ad_control(current,selected);self.clock.now+=3
        last=copy.deepcopy(first)
        for control in last['controls']:control['window_id']+=50
        self.assertIsNone(c.ad_close_control(last))
        self.assertEqual(sum(a[0]=='native_tap'for a in d.actions),2)
    def test_new_accessibility_window_id_does_not_unblock_redirecting_target(self):
        d=Replay(W_AD,on_tap=STORE);c=self.cycle(d)
        c.close_ad_control(W_AD,c.ad_close_control(W_AD));self.clock.now+=3
        self.assertTrue(c.return_from_ad_landing(STORE))
        changed=copy.deepcopy(W_AD)
        for control in changed['controls']:control['window_id']+=100
        self.assertIsNone(c.ad_close_control(changed))
    def test_failure_predicates_use_held_window_without_device_reads(self):
        d=Replay(MAIN);c=self.cycle(d)
        c.last_ad_window={**MAIN,'windows':[{'window_id':99,'window_layer':1,'window_type':'TYPE_APPLICATION',
                                           'window_bounds':[0,0,200,356],'window_active':True,'window_focused':True,'display_id':0}]}
        error=c.ad_failure('No unique Connect control was found')
        diagnostic=json.loads(str(error).split(' | predicates=',1)[1])
        self.assertEqual(diagnostic['connect_label_matches'],1)
        self.assertFalse(diagnostic['terms']);self.assertFalse(diagnostic['notification'])
        self.assertEqual(diagnostic['active_windows'][0]['window_id'],99)
        self.assertFalse(d.actions)
    def test_new_creative_with_same_close_id_gets_new_budget(self):
        d=Replay(ad());c=self.cycle(d)
        for letter in ('A','B','C'):
            w=ad();w['controls'][0]['content_desc']='Advertisement '+letter
            self.assertIsNotNone(c.ad_close_control(w));c.close_ad_control(w,w['controls'][-1])
            self.clock.now+=3
        self.assertEqual(sum(a[0]=='native_tap'for a in d.actions),3)
    def test_expired_provenance_is_not_reused(self):
        d=Replay(ad());c=self.cycle(d);c.close_ad_control(d.window,d.window['controls'][-1]);self.clock.now=121
        self.assertFalse(c.return_from_ad_landing(STORE));self.assertNotIn(('native_tap',42,345),d.actions)
    def test_return_does_not_loop_when_store_stays_foreground(self):
        d=Replay(ad(),on_tap=STORE,on_back=STORE);c=self.cycle(d)
        with self.assertRaisesRegex(RuntimeError,'return limit exhausted'):c.dismiss_ads()
        self.assertEqual(d.actions.count(('native_tap',42,345)),1)
    def test_store_without_verified_navigation_control_never_uses_coordinates(self):
        for field,value in [('resource_id','wrong'),('package','wrong'),('clickable',False),
                            ('window_type','TYPE_APPLICATION'),('window_title','Wrong'),('display_id',3)]:
            nav=dict(m.Cycle.observed_navigation_back(STORE));nav[field]=value
            store={**STORE,'navigation_controls':[nav]};d=Replay(ad(),on_tap=store);c=self.cycle(d)
            with self.assertRaisesRegex(RuntimeError,'usable close control'):c.dismiss_ads()
            self.assertNotIn(('native_tap',42,345),d.actions)
    def test_stale_or_missing_navigation_metadata_is_not_actionable(self):
        self.assertIsNone(m.Cycle.observed_navigation_back({**STORE,'stable':False}))
        self.assertIsNone(m.Cycle.observed_navigation_back({**STORE,'display_id':None}))
        self.assertIsNone(m.Cycle.observed_navigation_back({**STORE,'navigation_controls':[]}))
    def test_observe_requests_all_windows(self):
        d=Replay(MAIN);c=self.cycle(d)
        with patch.object(d,'inspect_window',return_value=MAIN)as inspect:
            c.observe()
        inspect.assert_called_once_with(windows=True,priority=m.UI_INSPECTION_PRIORITY)
    def test_native_only_no_adb_tap_fallback(self):
        d=Replay(ad());c=self.cycle(d)
        with patch.object(m,'TAP_BACKEND','adb'):
            with self.assertRaisesRegex(ValueError,'official Xenoid'):c.tap_point(10,10,'close')
        self.assertFalse(d.actions)

if __name__=='__main__':unittest.main()
