"""Current-only Android window reads, including the real host13/device1 ANR.

The compressed fixture is exact `dumpsys window displays`, captured read-only
2026-11-20T19:13:47Z. The archived section headers/foci below preserve the
separate historical and live sections of the real default window dump.
"""
import ast
import base64
import gzip
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h

CURRENT=gzip.decompress(base64.b64decode('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')).decode()
HISTORY='''WINDOW MANAGER LAST ANR (dumpsys window lastanr)
  ANR time: 21 Nov 2026 1:38:58 am
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0 (organized)
  mCurrentFocus=Window{2793225 u0 ImmersiveModeConfirmation}
  mFocusedApp=ActivityRecord{b3a96ce u0 dev.xenoid.daemon/.MainActivity} t9}
WINDOW MANAGER POLICY STATE (dumpsys window policy)
WINDOW MANAGER ANIMATOR STATE (dumpsys window animator)
WINDOW MANAGER SESSIONS (dumpsys window sessions)
'''
OTHER='  Display: mDisplayId=1 (organized)\n  mCurrentFocus=Window{abcd u0 other.app/.Other}\n'
XML='<hierarchy><node package="android" resource-id="android:id/aerr_close" text="Close app" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'
PROFILE={'input':{'name':'sec_touchscreen','x':{'minimum':0,'maximum':1439},'y':{'minimum':0,'maximum':3119}}}
INPUT=Path(__file__).with_name('test_native_input_fixture.txt').read_text()
EXPECTED=('d765dcd','0','','Application Not Responding: system')

class CurrentWindowTests(unittest.TestCase):
 def broker(self,**options):return h.Broker(None,lambda *_args:None,config={'command_timeout':0,**options})
 def test_real_current_only_anr_focus_is_accepted(self):self.assertEqual(h.foreground_window(CURRENT),EXPECTED)
 def test_real_default_dump_history_and_live_are_not_actionable(self):self.assertIsNone(h.foreground_window(HISTORY+CURRENT))
 def test_archived_only_display_is_not_mistaken_for_current(self):self.assertIsNone(h.foreground_window(HISTORY))
 def test_two_display_content_sections_reject_even_without_lastanr_header(self):
  self.assertIsNone(h.foreground_window(HISTORY[HISTORY.index('WINDOW MANAGER DISPLAY CONTENTS'):]+CURRENT))
 def test_duplicate_focus_within_selected_display_rejects(self):
  self.assertIsNone(h.foreground_window(CURRENT+'\n  mCurrentFocus=Window{abcd u0 other.app/.Main}\n'))
 def test_duplicate_selected_display_blocks_reject(self):
  self.assertIsNone(h.foreground_window(CURRENT+'\n  Display: mDisplayId=0 (organized)\n'))
 def test_other_display_does_not_steal_default_focus(self):self.assertEqual(h.foreground_window(CURRENT+OTHER),EXPECTED)
 def test_selected_display_comes_from_mapping_not_first_or_top_focused(self):
  self.assertEqual(h.foreground_window('mTopFocusedDisplayId=0\n'+CURRENT+OTHER,1),('abcd','0','other.app','.Other'))
 def test_duplicates_on_other_display_do_not_break_selected_display(self):
  self.assertEqual(h.foreground_window(CURRENT+OTHER+'  mCurrentFocus=null\n'),EXPECTED)
 def test_missing_selected_display_or_invalid_selector_fail_closed(self):
  for selector in (2,-1,True,'0'):
   with self.subTest(selector=selector):self.assertIsNone(h.foreground_window(CURRENT,selector))
 def test_null_selected_focus_does_not_choose_another_display(self):
  self.assertIsNone(h.foreground_window(CURRENT.replace('mCurrentFocus=Window{d765dcd u0 Application Not Responding: system}','mCurrentFocus=null')+OTHER))
 def test_native_snapshot_queries_current_displays_and_reports_real_anr(self):
  b=self.broker();response=json.dumps(PROFILE).encode()+b'\nXENOID_INPUT_MAPPING\n'+INPUT.encode()+b'\nXENOID_FOREGROUND_WINDOW\n'+CURRENT.encode()
  b.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,response,b''))
  result=b.native_interaction_snapshot(None,None,foreground=True)
  self.assertTrue(result['ready']);self.assertTrue(result['foreground']['ready']);self.assertEqual(result['foreground']['package'],'')
  self.assertEqual(result['mapping']['display_id'],0);self.assertEqual(b._native_display_id,0)
  self.assertIn('dumpsys window displays',b.native_display_exec.call_args.args[1][-1])
 def test_native_snapshot_selects_actual_mapping_display_id(self):
  b=self.broker();response=json.dumps(PROFILE).encode()+b'\nXENOID_INPUT_MAPPING\ninput'+b'\nXENOID_FOREGROUND_WINDOW\n'+(CURRENT+OTHER).encode()
  b.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,response,b''))
  with patch.object(h,'native_touch_point',return_value=(0,0,{'display_id':1})):
   result=b.native_interaction_snapshot(None,None,foreground=True)
  self.assertEqual(b._native_display_id,1);self.assertEqual(result['foreground']['package'],'other.app')
 def test_focus_command_uses_current_only_with_or_without_priority(self):
  b=self.broker();self.assertEqual(b.focus_command(),['shell','dumpsys','window','displays'])
  self.assertEqual(b.focus_command(-10),['shell','nice','-n','-10','dumpsys','window','displays'])
 def test_bounded_unlimited_and_nice_hierarchy_postfocus_read_current_only(self):
  for seconds,priority in ((0,None),(30,None),(30,-10)):
   with self.subTest(seconds=seconds,priority=priority):
    b=self.broker(command_timeout=seconds);b.adb=MagicMock(return_value=XML+'\nXENOID_UI_FOCUS_AFTER\n'+CURRENT)
    b.ui_hierarchy(None,priority,reader='legacy');command=b.adb.call_args.args[0][1]
    self.assertIn('dumpsys window displays',command);self.assertEqual(b._hierarchy_focus,EXPECTED)
 def test_inspector_stays_stable_on_actual_anr_and_exposes_real_close_control(self):
  b=self.broker();b.adb=MagicMock(side_effect=[CURRENT,XML+'\nXENOID_UI_FOCUS_AFTER\n'+CURRENT])
  result=b.inspect_window(reader='legacy');self.assertTrue(result['stable']);self.assertEqual(result['component'],'Application Not Responding: system')
  self.assertEqual(result['controls'][0]['resource_id'],'android:id/aerr_close');self.assertEqual(b.adb.call_count,2)
 def test_inspector_fallback_focus_queries_both_use_current_only(self):
  b=self.broker();b.adb=MagicMock(return_value=CURRENT);b.ui_hierarchy=lambda *_args:XML
  self.assertTrue(b.inspect_window()['stable']);self.assertEqual([call.args[0] for call in b.adb.call_args_list],[b.focus_command()]*2)
 def test_inspector_uses_last_proven_native_display(self):
  b=self.broker();b._native_display_id=1;b.adb=MagicMock(side_effect=[CURRENT+OTHER,XML+'\nXENOID_UI_FOCUS_AFTER\n'+CURRENT+OTHER])
  result=b.inspect_window(reader='legacy');self.assertTrue(result['stable']);self.assertEqual(result['package'],'other.app')
 def test_legacy_foreground_package_resolution_queries_current_only(self):
  b=self.broker();b.adb=MagicMock(return_value=CURRENT.replace('Window{d765dcd u0 Application Not Responding: system}','Window{d765dcd u0 target.app/.Main}'))
  self.assertEqual(b.verify_test_app(),'target.app');b.adb.assert_called_once_with(['shell','dumpsys','window','displays'])
 def test_sdk_source_contains_no_unfiltered_focus_query(self):
  # Inspect literal shell fragments and argument lists, covering future reader additions.
  tree=ast.parse(Path(h.__file__).read_text())
  navigation_guard=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='native_navigation_guard')
  navigation_nodes={id(node) for node in ast.walk(navigation_guard)}
  routing_queries=0
  for node in ast.walk(tree):
   if isinstance(node,ast.Constant) and isinstance(node.value,str) and 'dumpsys window' in node.value:
    self.assertNotRegex(node.value,r'dumpsys window(?! displays)')
   if isinstance(node,ast.List) and all(isinstance(item,ast.Constant) and isinstance(item.value,str) for item in node.elts):
    values=[item.value for item in node.elts]
    if 'dumpsys' in values and 'window' in values:
     getter=values[values.index('window')+1]
     if getter=='windows':
      # Window metadata is a separate input-routing proof. It must never
      # become the focus getter (which includes archived ANR focus entries).
      self.assertIn(id(node),navigation_nodes);self.assertEqual(values,['dumpsys','window','windows']);routing_queries+=1
     else:self.assertEqual(getter,'displays')
  self.assertEqual(routing_queries,1)

if __name__=='__main__':unittest.main()
