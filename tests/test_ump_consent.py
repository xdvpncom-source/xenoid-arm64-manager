"""Actual UMP sheet: observed decline only, official native input and fresh UI."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.modules['xenoid_device']=types.SimpleNamespace(Device=object)
spec=importlib.util.spec_from_file_location('ump_cycle',ROOT/'qa/vpn-cycle.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
WINDOW=json.loads((ROOT/'tests/fixtures/ad_windows/actual-ump-consent.json').read_text())['window']
MAIN={'stable':True,'package':'v2.vpn.fast','component':'v2.vpn.fast/com.xdvpn.app.MainActivity','controls':[{'text':'Connect','content_desc':'','resource_id':'','class_name':'android.widget.Button','enabled':True,'clickable':True,'bounds':[400,800,680,1080],'package':'v2.vpn.fast'}]}

class Clock:
 def __init__(self):self.now=0
 def monotonic(self):return self.now
 def sleep(self,seconds):self.now+=seconds

class Device:
 def __init__(self,clock,window):self.clock=clock;self.window=deepcopy(window);self.actions=[];self.logs=[];self.reads=[]
 def settings(self):return {'android_user':0}
 def log(self,value):self.logs.append(value)
 def inspect_window(self,**kwargs):self.reads.append(deepcopy(self.window));return self.window
 def tap(self,x,y):self.actions.append(('official_native_tap',self.clock.now,x,y));self.window=deepcopy(MAIN)
 def adb(self,*args):raise AssertionError('No ADB input or Back fallback')

def cycle(window=WINDOW):
 clock=Clock();device=Device(clock,window);c=m.Cycle(device,'v2.vpn.fast');c.service_crash_once=lambda:False;c.check_target_crash=lambda:None
 return clock,device,c

class ConsentTests(unittest.TestCase):
 def test_actual_title_and_choices_select_only_observed_negative(self):
  chosen=m.ump_decline_control(WINDOW);self.assertEqual(chosen['text'],'Do not consent');self.assertEqual(chosen['bounds'],[97,1832,984,1937]);self.assertTrue(chosen['clickable'])
  clock,d,c=cycle()
  with patch.object(m,'time',clock):self.assertTrue(c.service_dialog(WINDOW,ads=True))
  self.assertEqual(d.actions,[('official_native_tap',0,540,1884)]);self.assertIn('Input sent: decline UMP personal-data consent',d.logs)
 def test_observed_bounds_move_no_fixed_coordinate_fallback(self):
  w=deepcopy(WINDOW)
  for item in w['controls']:
   if item.get('text')=='Do not consent':item['bounds']=[200,1500,600,1600]
  clock,d,c=cycle(w)
  with patch.object(m,'time',clock):c.service_dialog(w,ads=True)
  self.assertEqual(d.actions,[('official_native_tap',0,400,1550)])
 def test_unique_decline_ambiguous_or_disabled_stays_blocked_without_action(self):
  for kind in ('ambiguous','disabled','two_windows','unknown_metadata'):
   w=deepcopy(WINDOW);negative=next(c for c in w['controls']if c.get('text')=='Do not consent')
   if kind=='ambiguous':w['controls'].append({**negative,'bounds':[100,1400,900,1500]})
   elif kind=='disabled':negative['enabled']=False
   elif kind=='two_windows':w['controls'] += [{**c,'window_id':999,'bounds':[v+1 for v in c['bounds']]}for c in deepcopy(w['controls'])]
   else:
    for c in w['controls']:
     for name in ('window_id','display_id','window_active','window_focused'):c.pop(name,None)
   self.assertTrue(m.is_ump_consent(w));self.assertTrue(m.is_terms(w));self.assertFalse(m.is_ad(w));self.assertIsNone(m.ump_decline_control(w))
   clock,d,c=cycle(w)
   with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w,ads=True))
   self.assertEqual(d.actions,[])
 def test_foreground_consent_for_another_app_is_not_accepted_or_declined(self):
  w=deepcopy(WINDOW);w['package']='other.app'
  for item in w['controls']:item['package']='other.app'
  self.assertTrue(m.is_ump_consent(w));clock,d,c=cycle(w)
  with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w,ads=True))
  self.assertEqual(d.actions,[])
 def test_nonmodal_footer_other_title_or_foreign_inactive_controls_never_declined(self):
  variants=[]
  for mode in ('footer','foreign','inactive','unstable'):
   w=deepcopy(WINDOW)
   if mode=='footer':
    for item in w['controls']:
     if 'asks for your consent'in item.get('text',''):item['text']='Privacy policy'
   elif mode=='foreign':
    for item in w['controls']:item['package']='other.app'
   elif mode=='inactive':
    for item in w['controls']:item['window_active']=False
   else:w['stable']=False
   variants.append(w)
  for w in variants:
   self.assertFalse(m.is_ump_consent(w));self.assertIsNone(m.ump_decline_control(w));clock,d,c=cycle(w)
   with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w))
   self.assertEqual(d.actions,[])
 def test_ad_dismissal_defers_to_requested_pause_then_declines_and_rechecks(self):
  w=deepcopy(WINDOW);w['controls'].append(deepcopy(MAIN['controls'][0]));clock,d,c=cycle(w)
  with patch.object(m,'time',clock):
   c.dismiss_ads();self.assertEqual(d.actions,[])
   c.wait(5);self.assertEqual(d.actions,[])
   ready=c.prepare_screen(10)
   self.assertEqual(d.actions,[('official_native_tap',5,540,1884)])
   self.assertEqual(ready,MAIN);self.assertGreaterEqual(clock.now,7);self.assertGreaterEqual(len(d.reads),3)
   c.connect(ready)
  self.assertEqual(d.actions[1][2:],(540,940));self.assertGreaterEqual(d.actions[1][1],7)
 def test_loading_words_inside_sheet_cannot_bypass_dialog_handling(self):
  w=deepcopy(WINDOW);w['controls'].append({**MAIN['controls'][0],'text':'Refreshing server list'})
  self.assertFalse(m.is_loading(w,'v2.vpn.fast'));self.assertFalse(m.is_ad(w));self.assertTrue(m.is_terms(w))
  clock,d,c=cycle(w)
  with patch.object(m,'time',clock):self.assertTrue(c.service_dialog(w,ads=True))
  self.assertEqual(len(d.actions),1)

if __name__=='__main__':unittest.main()
