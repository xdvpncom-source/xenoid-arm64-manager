"""Run the actual Java helper against small Android API doubles, without Android.

These exercise cache API calls, bounded traversal, cleanup and the framed
protocol; they do not qualify the installed platform API or a real device.
"""
import os
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAGIC = 0x58554931
STUBS = {
'android/graphics/Rect.java': '''package android.graphics;
public class Rect { public int left,top,right,bottom; }''',
'android/accessibilityservice/AccessibilityServiceInfo.java': '''package android.accessibilityservice;
public class AccessibilityServiceInfo { public int flags=0;
public static int FLAG_RETRIEVE_INTERACTIVE_WINDOWS=64,FLAG_REPORT_VIEW_IDS=16,FLAG_INCLUDE_NOT_IMPORTANT_VIEWS=2; }''',
'fixture/Trace.java': '''package fixture;
import java.nio.file.*;import java.nio.charset.StandardCharsets;
public class Trace { public static String mode=System.getProperty("fixture.mode", "ok");
 public static void add(String s) { try {Files.write(Paths.get(System.getProperty("fixture.trace")),
 (s+"\\n").getBytes(StandardCharsets.US_ASCII),StandardOpenOption.CREATE,StandardOpenOption.APPEND);}
 catch(Exception e){throw new RuntimeException(e);} } }''',
'android/util/SparseArray.java': '''package android.util;import java.util.*;
public class SparseArray { private Object values=make();
private static Object make(){List<Object> a=new ArrayList<Object>();
if(fixture.Trace.mode.equals("manywindows")){for(int i=0;i<18;i++)a.add(new android.view.accessibility.AccessibilityWindowInfo(true));}
else{a.add(new android.view.accessibility.AccessibilityWindowInfo(true));a.add(new android.view.accessibility.AccessibilityWindowInfo(false));}return a;}
public int size(){return fixture.Trace.mode.equals("emptydisplays")?0:1;}
public int keyAt(int index){if(fixture.Trace.mode.equals("keymetadatafail"))throw new RuntimeException("metadata");return 0;}
public Object valueAt(int index){return fixture.Trace.mode.equals("badlist")?"invalid":values;}
public Object get(int id) {return fixture.Trace.mode.equals("emptydisplays")?null:id==0?values:null;} }''',
'android/app/UiAutomation.java': '''package android.app;
import fixture.Trace;import android.accessibilityservice.AccessibilityServiceInfo;
public class UiAutomation {public AccessibilityServiceInfo getServiceInfo(){return new AccessibilityServiceInfo();}
public void setServiceInfo(AccessibilityServiceInfo i){if(i.flags!=82)throw new RuntimeException("flags");Trace.add("clear_cache_flags");}
public android.util.SparseArray getWindowsOnAllDisplays(){Trace.add("windows_metadata");return new android.util.SparseArray();}}''',
'com/android/uiautomator/core/UiAutomationShellWrapper.java': '''package com.android.uiautomator.core;
import fixture.Trace;public class UiAutomationShellWrapper {
public void connect(){Trace.add("connect");}
public android.app.UiAutomation getUiAutomation(){return new android.app.UiAutomation();}
public void disconnect(){Trace.add("disconnect");if(Trace.mode.equals("disconnectfail"))throw new RuntimeException("cleanup");}}''',
'android/view/accessibility/AccessibilityWindowInfo.java': '''package android.view.accessibility;
import fixture.Trace;import android.graphics.Rect;
public class AccessibilityWindowInfo {public static int TYPE_SYSTEM=3;private boolean nav;
public AccessibilityWindowInfo(boolean nav){this.nav=nav;}
public int getType(){return nav?3:1;}public int getDisplayId(){return 0;}
public CharSequence getTitle(){return fixture.Trace.mode.equals("nonav")&&nav?"Other system window":(fixture.Trace.mode.equals("longtitles")||fixture.Trace.mode.equals("manywindows"))&&nav?new String(new char[]{(char)34,(char)92,(char)10,(char)0x2603})+String.format("%0500d",0):nav?"Navigation bar":"PRIVATE APPLICATION TITLE";}
public int getId(){return Trace.mode.equals("renumbered")?16:8;}public int getLayer(){return 10;}
public void getBoundsInScreen(Rect r){r.left=0;r.top=90;r.right=100;r.bottom=100;}
public AccessibilityNodeInfo getRoot(int strategy){if(!nav)throw new RuntimeException("app_root_requested");
if(strategy!=0)throw new RuntimeException("prefetch");Trace.add("root_bypass_0");
return Trace.mode.equals("nullroot")?null:new AccessibilityNodeInfo(0);}
public void recycle(){Trace.add(nav?"recycle_nav_window":"recycle_app_window");}}''',
'android/view/accessibility/AccessibilityNodeInfo.java': '''package android.view.accessibility;
import fixture.Trace;import android.graphics.Rect;
public class AccessibilityNodeInfo {private int index;
public AccessibilityNodeInfo(int i){index=i;}
public boolean refresh(){Trace.add("refresh_"+index);return !Trace.mode.equals("obsolete")||index==0;}
public String getViewIdResourceName(){return index==0?"com.android.systemui:id/nav_buttons":"com.android.systemui:id/back";}
public CharSequence getPackageName(){return "com.android.systemui";}
public boolean isVisibleToUser(){return !Trace.mode.equals("hidden");}
public boolean isEnabled(){return true;}public boolean isClickable(){return index>0;}
public int getWindowId(){return Trace.mode.equals("wrongwindow")?9:Trace.mode.equals("renumbered")?16:8;}
public int getChildCount(){return index==0?(Trace.mode.equals("bound")?129:Trace.mode.equals("duplicate")||Trace.mode.equals("childfail")?2:1):0;}
public AccessibilityNodeInfo getChild(int i,int strategy){if(strategy!=0)throw new RuntimeException("prefetch");
if(Trace.mode.equals("childfail")&&i==1)throw new RuntimeException("provider");
Trace.add("child_bypass_0");return new AccessibilityNodeInfo(i+1);}
public void getBoundsInScreen(Rect r){r.left=0;r.top=90;r.right=index==0?100:20;r.bottom=100;}
public void recycle(){Trace.add("recycle_node_"+index);}}''',
'com/android/commands/uiautomator/DumpCommand.java': '''package com.android.commands.uiautomator;
import java.nio.file.*;import java.nio.charset.StandardCharsets;import fixture.Trace;
public class DumpCommand {public void run(String[] args){try{Trace.add("stock_dump_"+(args.length-1));
Files.write(Paths.get(args[args.length-1]),"<hierarchy/>".getBytes(StandardCharsets.US_ASCII));}
catch(Exception e){throw new RuntimeException(e);}}}''',
}


@unittest.skipUnless(shutil.which('javac') and shutil.which('java'), 'Existing JDK required')
class JavaNavigationProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='xenoid-nav-java-fixture-')
        cls.root = Path(cls.temporary.name)
        cls.classes = cls.root / 'classes'
        cls.classes.mkdir()
        paths = []
        for name, source in STUBS.items():
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            paths.append(str(path))
        result = subprocess.run([shutil.which('javac'), '-J-Xmx256m', '--release', '8', '-d', str(cls.classes),
            str(ROOT / 'native/xenoid-ui-reader/StockUiServer.java'), *paths],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        if result.returncode:
            raise AssertionError(result.stderr.decode())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_helper(self, mode='ok', nav_requests=1):
        with tempfile.TemporaryDirectory(prefix='xenoid-nav-pipe-') as temporary:
            private = Path(temporary) / 'private'
            private.mkdir(mode=0o700)
            private = private.resolve()
            trace = Path(temporary) / 'trace'
            request = (struct.pack('>IIB', MAGIC, 1, 2) + b''.join(struct.pack('>IIBi', MAGIC, index+2, 4, 0)for index in range(nav_requests))
                       + struct.pack('>IIB', MAGIC, nav_requests+2, 0) + struct.pack('>IIB', MAGIC, nav_requests+3, 127))
            result = subprocess.run([shutil.which('java'), '-Xmx128m', '-Dfixture.mode=' + mode,
                '-Dfixture.trace=' + str(trace), '-cp', str(self.classes), 'org.xenoid.qa.StockUiServer', str(private)],
                input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            frames, data = [], result.stdout
            while data:
                self.assertGreaterEqual(len(data), 13)
                magic, sequence, status, count = struct.unpack('>IIBI', data[:13])
                self.assertEqual(magic, MAGIC)
                self.assertLessEqual(count, 16384)
                self.assertGreaterEqual(len(data), 13 + count)
                frames.append((sequence, status, data[13:13+count]))
                data = data[13+count:]
            self.assertEqual(list(private.iterdir()), [], 'Per-request XML must be deleted')
            self.last_stderr = result.stderr
            return frames, trace.read_text().splitlines()

    def test_real_java_protocol_fresh_cache_reads_only_nav_and_preserves_legacy(self):
        frames, trace = self.run_helper()
        self.assertEqual([(seq, status) for seq, status, _ in frames], [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)])
        self.assertIn(b'"cache_bypass":"new_connection_setServiceInfo_root_target_refresh"', frames[2][2])
        proof = json.loads(frames[2][2])
        self.assertEqual(proof['node_window_id'], proof['window_id'])
        self.assertEqual(proof['window_id_scope'], 'current_ui_automation_connection')
        self.assertIn('clear_cache_flags', trace)
        self.assertIn('root_bypass_0', trace)
        self.assertIn('child_bypass_0', trace)
        self.assertEqual(trace.count('refresh_1'), 2, 'Target must be explicitly refreshed after traversal')
        self.assertEqual(trace.count('connect'), 1)
        self.assertEqual(trace.count('disconnect'), 1)
        self.assertEqual(trace.count('stock_dump_1'), 1)
        self.assertEqual(trace.count('stock_dump_0'), 1)
        self.assertIn('recycle_node_0', trace)
        self.assertIn('recycle_node_1', trace)
        self.assertIn('recycle_nav_window', trace)
        self.assertIn('recycle_app_window', trace)

    def test_ambiguous_obsolete_hidden_null_and_bound_nodes_fail_then_allow_next_stock_dump(self):
        for mode in ('duplicate', 'obsolete', 'hidden', 'nullroot', 'bound', 'childfail', 'wrongwindow'):
            with self.subTest(mode=mode):
                frames, trace = self.run_helper(mode)
                self.assertEqual(frames[2][1], 1)
                self.assertEqual(frames[3][:2], (3, 0))
                self.assertEqual(trace.count('disconnect'), 1)
                self.assertIn('recycle_nav_window', trace)
                if mode == 'childfail':
                    self.assertIn('recycle_node_1', trace, 'Queued children must be recycled after a later provider error')

    def test_renumbered_connection_reports_current_window_and_node_identity(self):
        frames, trace = self.run_helper('renumbered')
        self.assertEqual(frames[2][1], 0)
        proof = json.loads(frames[2][2])
        self.assertEqual((proof['window_id'], proof['node_window_id']), (16, 16))
        self.assertEqual(proof['window_id_scope'], 'current_ui_automation_connection')
        self.assertEqual(trace.count('connect'), 1)
        self.assertEqual(trace.count('disconnect'), 1)

    def diagnostic(self):
        rows=[line for line in self.last_stderr.splitlines()if line.startswith(b'XENOID_NAV_DIAGNOSTIC ')]
        self.assertEqual(len(rows),1,'Exactly one labelled row per navigation request')
        self.assertLessEqual(len(rows[0])+1,4096)
        self.assertTrue(all(value<128 for value in rows[0]))
        value=json.loads(rows[0].split(b' ',1)[1]);self.assertEqual(value['sequence'],2);self.assertEqual(value['display_id'],0)
        return value

    def test_distinct_window_failure_branches_keep_protocol_cleanup_and_metadata(self):
        for mode,code in [('badlist','nav_display_value_invalid'),('emptydisplays','nav_display_unavailable'),('nonav','nav_bar_unavailable')]:
            with self.subTest(mode=mode):
                frames,trace=self.run_helper(mode);self.assertEqual(frames[2],(2,1,code.encode()))
                self.assertEqual(frames[3][:2],(3,0));self.assertEqual(trace.count('disconnect'),1)
                self.assertNotIn('root_bypass_0',trace,'Failure metadata must never read any root')
                diag=self.diagnostic();self.assertEqual(diag['outcome'],code)
                self.assertNotIn(b'PRIVATE APPLICATION TITLE',self.last_stderr)
                if mode=='badlist':self.assertEqual(diag['displays'][0]['value_type'],'String')
                if mode=='emptydisplays':self.assertEqual(diag['display_count'],0)
                if mode=='nonav':self.assertEqual(diag['windows_seen'],2)

    def test_success_metadata_is_sequence_bound_bounded_and_omits_app_titles(self):
        frames,trace=self.run_helper();diag=self.diagnostic()
        self.assertEqual(diag['outcome'],'ok');self.assertEqual(diag['display_count'],1)
        self.assertEqual(diag['displays'][0]['key'],0);self.assertEqual(diag['windows_seen'],2)
        self.assertEqual(diag['windows_recorded'],2);self.assertFalse(diag['truncated'])
        self.assertEqual(diag['windows'][0]['system_title'],'Navigation bar')
        self.assertNotIn('system_title',diag['windows'][1])
        self.assertNotIn(b'PRIVATE APPLICATION TITLE',self.last_stderr)

    def test_long_titles_and_window_count_are_bounded_ASCII_escaped(self):
        for mode,count in [('longtitles',2),('manywindows',18)]:
            with self.subTest(mode=mode):
                frames,trace=self.run_helper(mode);diag=self.diagnostic()
                self.assertEqual(frames[2],(2,1,b'nav_bar_unavailable'))
                self.assertTrue(diag['truncated']);self.assertEqual(diag['windows_seen'],count)
                self.assertLessEqual(diag['windows_recorded'],12)
                self.assertTrue(diag['windows'][0]['system_title'].startswith(chr(34)+chr(92)+chr(10)+chr(0x2603)))
                self.assertNotIn('root_bypass_0',trace)
                self.assertEqual(trace.count('recycle_nav_window'),count if mode=='manywindows'else 1)

    def test_observability_metadata_failure_does_not_change_successful_proof(self):
        frames,trace=self.run_helper('keymetadatafail');diag=self.diagnostic()
        self.assertEqual(frames[2][1],0);self.assertTrue(diag['metadata_error'])
        self.assertEqual(diag['outcome'],'ok');self.assertEqual(frames[1][2],b'<hierarchy/>')
        self.assertEqual(frames[3][2],b'<hierarchy/>');self.assertEqual(frames[-1],(4,0,b'BYE'))

    def test_multiple_navigation_requests_have_one_row_each_with_exact_sequence(self):
        frames,trace=self.run_helper(nav_requests=2)
        rows=[json.loads(line.split(b' ',1)[1])for line in self.last_stderr.splitlines()if line.startswith(b'XENOID_NAV_DIAGNOSTIC ')]
        self.assertEqual([row['sequence']for row in rows],[2,3]);self.assertEqual(len(frames),6)
        self.assertTrue(all(row['display_id']==0 and row['outcome']=='ok'for row in rows))
        self.assertEqual(trace.count('connect'),2);self.assertEqual(trace.count('disconnect'),2)

    def test_disconnect_failure_retires_pipe_instead_of_reusing_leaked_connection(self):
        frames, trace = self.run_helper('disconnectfail')
        self.assertEqual(frames[-1], (2, 2, b'nav_cleanup_failed'))
        self.assertEqual(trace.count('connect'), 1)
        self.assertEqual(trace.count('disconnect'), 1)
        self.assertNotIn('stock_dump_0', trace)


if __name__ == '__main__':
    unittest.main()
