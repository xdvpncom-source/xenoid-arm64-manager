import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.modules['xenoid_device'] = types.SimpleNamespace(Device=object)
spec=importlib.util.spec_from_file_location('cycle',Path(__file__).resolve().parents[1]/'qa/vpn-cycle.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Clock:
    def __init__(self): self.now=0
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds

def control(text='',rid='',box=(10,10,30,30),cls='android.widget.Button',package='qa.vpn'):
    return dict(text=text,content_desc='',resource_id=rid,bounds=list(box),class_name=cls,package=package,enabled=True,clickable=True)
def window(kind,items,package='qa.vpn'):
    return dict(stable=True,package=package,component=package+'/'+kind,controls=items)

class FakeDevice:
    def __init__(self,clock):self.clock=clock;self.trace=[];self.state='closed';self.package='qa.vpn';self.allowed=False;self.display_size=None;self.display_density=None
    def settings(self):return {'package':self.package,'android_user':0}
    def configure_run(self,**kwargs):self.trace.append(('configure',self.clock.now,kwargs))
    def log(self,value):self.trace.append(('log',self.clock.now,value))
    def app_package(self):
        if not self.package:raise RuntimeError('Assigned APK unavailable')
        return self.package
    def app_open(self):self.trace.append(('open',self.clock.now));self.state='startup'
    def app_close(self):self.trace.append(('close',self.clock.now));self.state='closed'
    def set_frame_rate(self,fps,timeout=None):self.trace.append(('fps',self.clock.now,fps,timeout));return {'fps':fps,'changed':False}
    def clear_data(self,package,user):self.trace.append(('clear',self.clock.now,package,user));return 'Success'
    def back(self):self.trace.append(('back',self.clock.now));self.state='notification'
    def inspect_window(self, *, priority=None):
        self.trace.append(('inspect',self.clock.now,priority))
        if self.state=='startup':
            return window('AdActivity',[control('Advertisement',box=(0,0,1000,1000)),control('Close',box=(900,10,940,50))])
        if self.state=='terms':return window('MainActivity',[control('Data collection disclosure'),control('Close',box=(900,60,940,100))])
        if self.state=='notification':return window('GrantPermissionsActivity',[control('Allow QA VPN to send notifications?'),control('Allow','com.android.permissioncontroller:id/permission_allow_button',box=(700,700,900,800))],package='com.android.permissioncontroller')
        if self.state=='vpn':return window('ConfirmDialog',[control('Connection request'),control('OK','android:id/button1',box=(700,900,900,1000))],package='com.android.vpndialogs')
        return window('MainActivity',[control('Connect',box=(400,400,600,600))])
    def tap(self,x,y):
        self.trace.append(('tap',self.clock.now,self.state,x,y))
        if self.state=='startup':self.state='terms'
        elif self.state=='terms':self.state='notification'
        elif self.state=='notification':self.state='main'
        elif self.state=='main':self.state='vpn'
        elif self.state=='vpn':self.state='connected';self.allowed=True
    def adb(self,args):
        self.trace.append(('adb',self.clock.now,args))
        if args[:3]==['shell','input','tap']:
            self.tap(int(args[3]),int(args[4]));return ''
        if args==m.CRASH_FOCUS_COMMAND:
            hint=getattr(self,'focus_hint',None)
            if callable(hint):return hint()
            return hint or 'mCurrentFocus=Window{abcd u0 '+self.package+'/.MainActivity}'
        if args[:3]==['shell','appops','get']:return 'ACTIVATE_VPN: allow' if self.allowed else 'ACTIVATE_VPN: default'
        if args==['shell','wm','size']:
            return 'Physical size: 1000x1000'+(f'\nOverride size: {self.display_size}' if self.display_size else '')
        if args==['shell','wm','density']:
            return 'Physical density: 400'+(f'\nOverride density: {self.display_density}' if self.display_density else '')
        if args[:3]==['shell','wm','size']:
            self.display_size=None if args[3]=='reset' else args[3];return ''
        if args[:3]==['shell','wm','density']:
            self.display_density=None if args[3]=='reset' else args[3];return ''
        if args[:5]==['shell','cmd','package','resolve-activity','--brief']:
            return 'com.android.launcher3/.Home'
        raise AssertionError(args)

class Tests(unittest.TestCase):
    def setUp(self):
        # These existing regressions isolate the app/UI cycle. The new optional
        # post-cycle identity stage has its own enabled-path integration tests.
        self.regeneration = patch.object(m, 'REGENERATE_ENABLED', False)
        self.regeneration.start()
        self.addCleanup(self.regeneration.stop)

    def test_adb_tap_remains_source_selectable(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock),patch.object(m,'TAP_BACKEND','adb'):
            c.tap(control('Close',box=(10,60,28,90)),'close')
        self.assertIn(('adb',0,['shell','input','tap','19','75']),d.trace)
    def test_default_tap_uses_official_native_input(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock):
            c.tap_point(19,75,'close')
        self.assertIn(('tap',0,'closed',19,75),d.trace)
        self.assertFalse(any(row[0]=='adb' for row in d.trace))
    def test_invalid_tap_backend_fails_without_input(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'TAP_BACKEND','unsupported'):
            with self.assertRaisesRegex(ValueError,'TAP_BACKEND'):c.tap_point(19,75,'close')
        self.assertFalse(any(row[0] in {'adb','tap'} for row in d.trace))
    def test_observe_passes_configured_ui_reader_priority(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'UI_INSPECTION_PRIORITY',-10):
            self.assertIsNotNone(c.observe())
        self.assertEqual([row[2] for row in d.trace if row[0]=='inspect'],[-10])
    def test_none_ui_priority_uses_legacy_no_keyword_call(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda:window('MainActivity',[])
        with patch.object(m,'UI_INSPECTION_PRIORITY',None):
            self.assertIsNotNone(c.observe())
    def test_exact_complete_order_timing_and_reset(self):
        clock=Clock();d=FakeDevice(clock)
        with patch.object(m,'time',clock),patch.object(m.random,'randint',return_value=97):m.Cycle(d,'qa.vpn').run()
        taps=[row for row in d.trace if row[0]=='tap']
        self.assertEqual([row[2] for row in taps],['startup','terms','notification','main','vpn'])
        self.assertEqual(taps[0][1],97)
        self.assertGreaterEqual(taps[1][1]-taps[0][1],5)
        closes=[row for row in d.trace if row[0]=='close'];clears=[row for row in d.trace if row[0]=='clear']
        self.assertEqual(len(closes),1);self.assertEqual(len(clears),1)
        approval_log=next(row for row in d.trace if row[0]=='log' and str(row[2]).startswith('Android VPN permission confirmed'))
        self.assertEqual(closes[0][1]-approval_log[1],100)
        self.assertEqual(clears[0][2:],('qa.vpn','0'))
    def test_assignment_missing_fails_before_open_or_clear(self):
        clock=Clock();d=FakeDevice(clock);d.package=''
        with patch.object(m,'Device',return_value=d),patch.object(m,'time',clock):
            with self.assertRaises(SystemExit) as failure:m.main()
        self.assertEqual(failure.exception.code,75)
        self.assertFalse(any(row[0] in {'open','clear'} for row in d.trace))
        self.assertEqual(clock.now,m.FAILED_CYCLE_BACKOFF_SECONDS)
    def test_each_device_resolves_its_assigned_apk(self):
        for package in ('qa.alpha','qa.beta'):
            clock=Clock();d=FakeDevice(clock);d.package=package
            with patch.object(m,'Device',return_value=d),patch.object(m,'Cycle') as factory:
                m.main()
            factory.assert_called_once_with(d,"")
            self.assertEqual(factory.return_value.package,package)
            factory.return_value.run.assert_called_once()
            self.assertEqual(d.trace[0],('configure',0,{'package':None,'verify_apk':True}))
    def test_non_notification_permission_not_accepted(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('GrantPermissionsActivity',[control('Allow camera access?'),control('Allow','com.android.permissioncontroller:id/permission_allow_button')],package='com.android.permissioncontroller')
        with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w))
        self.assertFalse(any(row[0]=='tap' for row in d.trace))
    def test_terms_never_accepts_agree_or_uses_unconfigured_back(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('MainActivity',[control('Data collection disclosure'),control('Agree and continue')])
        with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w))
        self.assertFalse(any(row[0] in {'tap','back'} for row in d.trace))
    def test_terms_unlabelled_close_uses_observed_header_button(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('MainActivity',[control('',box=(0,0,1000,1000),cls='android.view.View'),control('Data collection disclosure',box=(80,100,700,150)),control('',box=(10,100,60,150),cls='android.widget.ImageButton')])
        with patch.object(m,'time',clock):self.assertTrue(c.service_dialog(w))
        self.assertIn(('tap',0,'closed',35,125),d.trace)
    def test_footer_terms_links_are_not_a_modal(self):
        w=window('MainActivity',[control('Privacy policy'),control('Terms of service')])
        self.assertFalse(m.is_terms(w))
    def test_geometry_requires_identified_ad_and_actual_button(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        w=window('AdActivity',[control('Advertisement',box=(0,0,1000,1000)),control('',box=(900,10,940,50),cls='android.widget.ImageButton')])
        self.assertIsNotNone(c.close_control(w,geometric=m.is_ad(w)))
        main=window('MainActivity',[control('',box=(900,10,940,50),cls='android.widget.ImageButton')])
        self.assertIsNone(c.close_control(main,geometric=m.is_ad(main)))
    def test_ambiguous_named_closes_not_guessed(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        w=window('AdActivity',[control('Close',box=(10,10,50,50)),control('Close',box=(900,10,950,50))])
        self.assertIsNone(c.close_control(w))
    def test_previously_authorized_vpn_needs_no_new_dialog(self):
        clock=Clock();d=FakeDevice(clock);d.allowed=True;d.state='connected';c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock):c.approve_vpn()
        self.assertEqual(clock.now,0);self.assertFalse(any(row[0]=='tap' for row in d.trace))
    def test_loading_modal_blocks_vpn_success_even_when_already_authorized(self):
        clock=Clock();d=FakeDevice(clock);d.allowed=True;c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('MainActivity',[control('Refreshing server list'),
            control('Please wait while the app fetches enough servers.'),control('Connect')])
        with patch.object(m,'time',clock),patch.object(m,'VPN_READY_SECONDS',6):
            with self.assertRaisesRegex(RuntimeError,'server list loading did not finish'):c.approve_vpn()
        self.assertFalse(any(row[0]=='adb' and row[2][:3]==['shell','appops','get'] for row in d.trace))
        self.assertEqual(sum(row[0]=='log' and row[2]=='Waiting for app server list loading to finish' for row in d.trace),1)
        self.assertFalse(any(row[0]=='log' and 'permission confirmed' in row[2] for row in d.trace))
    def test_loading_transition_to_ready_allows_fresh_permission_check(self):
        clock=Clock();d=FakeDevice(clock);d.allowed=True;c=m.Cycle(d,'qa.vpn');reads=[]
        def inspect(**kwargs):
            reads.append(True)
            return window('MainActivity',[control('Almost ready to connect' if len(reads)<3 else 'Connected')])
        d.inspect_window=inspect
        with patch.object(m,'time',clock):c.approve_vpn()
        self.assertEqual(len(reads),3)
        self.assertEqual(sum(row[0]=='adb' and row[2][:3]==['shell','appops','get'] for row in d.trace),1)
        self.assertTrue(any(row[0]=='log' and 'permission confirmed' in row[2] for row in d.trace))
    def test_loading_modal_blocks_preparation_and_ad_readiness(self):
        for phase in ('prepare','ads'):
            clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
            d.inspect_window=lambda **kwargs:window('MainActivity',[control('Refreshing server list'),control('Connect')])
            with patch.object(m,'time',clock),patch.object(m,'AD_READY_SECONDS',6):
                with self.assertRaises(RuntimeError):
                    c.prepare_screen(6) if phase=='prepare' else c.dismiss_ads()
            self.assertFalse(any(row[0]=='tap' for row in d.trace))
    def test_loading_words_are_scoped_to_the_assigned_app(self):
        self.assertFalse(m.is_loading(window('Article',[control('Refreshing server list')],package='browser.app'),'qa.vpn'))
    def test_explicit_connect_position_is_used_when_configured(self):
        clock=Clock();d=FakeDevice(clock);d.state='main';c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock),patch.object(m,'CONNECT_POSITION',(500,500)):
            c.connect(window('MainActivity',[]))
        self.assertIn(('tap',0,'main',500,500),d.trace)
    def test_cycle_error_cleanup_then_return_for_panel_retry(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock),patch.object(c,'dismiss_ads',side_effect=RuntimeError('missing close')),patch.object(m.random,'randint',return_value=90):
            with self.assertRaisesRegex(RuntimeError,'missing close'):c.run()
        self.assertTrue(any(row[0]=='close' for row in d.trace));self.assertTrue(any(row[0]=='clear' for row in d.trace))
        self.assertTrue(any(row[0]=='log' and 'Cycle failed:' in str(row[2]) for row in d.trace))
    def test_appops_error_is_pending_not_success(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.adb=lambda args: (_ for _ in ()).throw(RuntimeError('No operation yet'))
        self.assertFalse(c.vpn_allowed())
        self.assertTrue(any(row[0]=='log' and 'Waiting for Android VPN' in row[2] for row in d.trace))
    def test_launch_failure_is_logged_cleaned_and_retryable(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.app_open=lambda: (_ for _ in ()).throw(RuntimeError('launch failed'))
        with patch.object(m,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'launch failed'):c.run()
        self.assertTrue(any(row[0]=='close' for row in d.trace))
        self.assertTrue(any(row[0]=='clear' for row in d.trace))
        self.assertTrue(any(row[0]=='log' and 'launch failed' in row[2] for row in d.trace))
    def test_cleanup_error_is_not_reported_success(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn');d.clear_data=lambda *a,**kw:'Failure'
        with self.assertRaises(RuntimeError):c.cleanup()
    def test_recurring_dialog_during_connected_wait(self):
        clock=Clock();d=FakeDevice(clock);d.state='notification';c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock):c.wait(5,dialogs=True,ads=True,vpn=True)
        self.assertTrue(any(row[0]=='tap' and row[2]=='notification' for row in d.trace));self.assertEqual(clock.now,5)

    def test_bluetooth_crash_close_prioritized_over_other_actions(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('AppErrorDialog',[
            control('Bluetooth keeps stopping'),
            control('App info','android:id/aerr_report',box=(10,10,90,50)),
            control('Close app','android:id/aerr_close',box=(100,100,200,160)),
            control('Allow','com.android.permissioncontroller:id/permission_allow_button',box=(700,700,900,800)),
        ],package='android')
        with patch.object(m,'time',clock):self.assertTrue(c.service_dialog(w,vpn=True,ads=True))
        taps=[row for row in d.trace if row[0]=='tap']
        self.assertEqual(taps,[('tap',0,'closed',150,130)])
    def test_crash_close_does_not_tap_app_info_if_close_missing(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('App info','android:id/aerr_report')],package='android')
        with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w,ads=True))
        self.assertFalse(any(row[0]=='tap' for row in d.trace))
    def test_crash_text_in_webpage_is_not_system_dialog(self):
        w=window('Article',[control('App keeps stopping'),control('Close app')],package='browser.app')
        self.assertFalse(m.is_crash_dialog(w))
        w=window('Article',[control('Bluetooth keeps stopping'),control('Close app')],package='browser.app')
        self.assertFalse(m.is_crash_dialog(w))
    def test_crash_detected_during_initial_wait_without_closing_ad_early(self):
        clock=Clock();d=FakeDevice(clock);d.state='startup';c=m.Cycle(d,'qa.vpn')
        original=d.inspect_window;seen=[False]
        def observe(**kwargs):
            if not seen[0]:
                return window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close',box=(10,10,30,30))],package='android')
            return original(**kwargs)
        original_tap=d.tap
        def tap(x,y):
            if not seen[0]:
                seen[0]=True;d.trace.append(('crash-close',clock.now,x,y))
            else:original_tap(x,y)
        d.inspect_window=observe;d.tap=tap
        d.focus_hint=lambda: ('mCurrentFocus=Window{abcd u0 Application Error: com.android.bluetooth}'
                             if not seen[0] else 'mCurrentFocus=Window{abcd u0 qa.vpn/.MainActivity}')
        with patch.object(m,'time',clock):c.wait(90)
        self.assertTrue(seen[0]);self.assertEqual(d.state,'startup')
        self.assertFalse(any(row[0]=='tap' for row in d.trace))
        self.assertEqual(clock.now,90)
    def test_crash_detected_during_post_ad_and_connected_waits(self):
        for seconds,dialogs in [(5,False),(100,True)]:
            clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
            d.inspect_window=lambda **kwargs:window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close')],package='android')
            d.focus_hint='mCurrentFocus=Window{abcd u0 Application Error: com.android.bluetooth}'
            with patch.object(m,'time',clock):c.wait(seconds,dialogs=dialogs,vpn=True,ads=True)
            self.assertTrue(any(row[0]=='tap' for row in d.trace));self.assertEqual(clock.now,seconds)
    def test_failed_cycle_requests_retry_instead_of_success(self):
        clock=Clock();d=FakeDevice(clock)
        with patch.object(m,'Device',return_value=d),patch.object(m,'time',clock),patch.object(m.Cycle,'run',side_effect=RuntimeError('bad UI')):
            with self.assertRaises(SystemExit) as failure:m.main()
        self.assertEqual(failure.exception.code,75)
        self.assertTrue(any(row[0]=='log' and 'not successful' in str(row[2]) for row in d.trace))
        self.assertFalse(any(row[0]=='log' and 'completed successfully' in str(row[2]) for row in d.trace))
    def test_close_error_does_not_skip_explicit_clear(self):
        d=FakeDevice(Clock());c=m.Cycle(d,'qa.vpn')
        d.app_close=lambda:(_ for _ in ()).throw(RuntimeError('force-stop timed out'))
        c.cleanup()
        self.assertTrue(any(row[0]=='clear' for row in d.trace))
        self.assertTrue(any(row[0]=='log' and 'recovered' in str(row[2]) for row in d.trace))
    def test_vpn_not_confirmed_from_stale_crash_window(self):
        clock=Clock();d=FakeDevice(clock);d.allowed=True;d.state='connected';c=m.Cycle(d,'qa.vpn')
        original=d.inspect_window
        d.inspect_window=lambda **kwargs:window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close')],package='android')
        with patch.object(m,'time',clock),patch.object(m,'VPN_READY_SECONDS',3):
            with self.assertRaisesRegex(RuntimeError,'VPN permission was not confirmed'):c.approve_vpn()
    def test_crash_input_transport_error_is_retryable(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.tap=lambda *args:(_ for _ in ()).throw(RuntimeError('input temporarily unavailable'))
        w=window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close')],package='android')
        with patch.object(m,'time',clock):self.assertFalse(c.service_dialog(w))

    def test_ordinary_initial_wait_does_not_dump_ui_hierarchy(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:(_ for _ in ()).throw(AssertionError('UI dump is not needed for ordinary dwell'))
        with patch.object(m,'time',clock):c.wait(90)
        self.assertEqual(clock.now,90)
        self.assertTrue(any(row[0]=='adb' and row[2]==m.CRASH_FOCUS_COMMAND for row in d.trace))
    def test_crash_focus_triggers_one_full_hierarchy_read(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn');reads=[]
        d.focus_hint='mCurrentFocus=Window{1234 u0 Application Error: com.android.bluetooth}'
        def ui(**kwargs):
            reads.append(True)
            return window('AppErrorDialog',[control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close')],package='android')
        d.inspect_window=ui
        with patch.object(m,'time',clock):self.assertTrue(c.service_crash_once())
        self.assertEqual(len(reads),1)
    def test_unknown_focus_fallback_is_rate_limited(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn');reads=[]
        d.focus_hint='mCurrentFocus=UnrecognizedWindow'
        d.inspect_window=lambda **kwargs:(reads.append(True) or window('MainActivity',[]))
        with patch.object(m,'time',clock):
            c.service_crash_once();clock.now+=1;c.service_crash_once()
            clock.now+=m.CRASH_FALLBACK_SECONDS;c.service_crash_once()
        self.assertEqual(len(reads),2)

    def test_three_modals_with_eight_second_ui_reads_fit_readiness(self):
        clock=Clock();d=FakeDevice(clock);d.state='notification';c=m.Cycle(d,'qa.vpn')
        original_observe=d.inspect_window
        def slow_observe(**kwargs):
            clock.now+=8
            return original_observe(**kwargs)
        def next_modal(x,y):
            d.trace.append(('tap',clock.now,d.state,x,y))
            d.state={'notification':'terms','terms':'startup','startup':'main'}[d.state]
        d.inspect_window=slow_observe;d.tap=next_modal
        with patch.object(m,'time',clock):
            ready=c.prepare_screen(m.DIALOG_READY_SECONDS)
        self.assertEqual(ready['package'],'qa.vpn')
        self.assertEqual(d.state,'main')
        self.assertEqual([row[2] for row in d.trace if row[0]=='tap'],['notification','terms','startup'])
        self.assertGreater(clock.now,45)
        self.assertLess(clock.now,m.DIALOG_READY_SECONDS)

    def test_inline_ad_below_connect_does_not_block_readiness(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('MainActivity',[control('Connect',box=(400,400,600,600)),
                                 control('Advertisement',box=(0,900,1000,1000))])
        d.inspect_window=lambda **kwargs:w
        self.assertTrue(m.is_ad(w))
        self.assertFalse(m.is_blocking_ad(w,'qa.vpn'))
        with patch.object(m,'time',clock):self.assertEqual(c.prepare_screen(10),w)
        self.assertFalse(any(row[0]=='tap' for row in d.trace))
    def test_real_ad_activity_blocks_even_with_underlying_connect(self):
        w=window('com.google.android.gms.ads.AdActivity',[
            control('Connect',box=(400,400,600,600)),control('Advertisement',box=(0,0,1000,1000))])
        self.assertTrue(m.is_blocking_ad(w,'qa.vpn'))
    def test_ad_webview_covering_connect_remains_blocking(self):
        w=window('MainActivity',[control('Connect',box=(400,400,600,600)),
                                 control('Advertisement',box=(0,100,1000,200)),
                                 control('',box=(0,0,1000,1000),cls='android.webkit.WebView')])
        self.assertTrue(m.is_blocking_ad(w,'qa.vpn'))
    def test_bottom_ad_webview_does_not_cover_connect(self):
        w=window('MainActivity',[control('Connect',box=(400,400,600,600)),
                                 control('Advertisement',box=(0,900,1000,1000)),
                                 control('',box=(0,800,1000,1000),cls='android.webkit.WebView')])
        self.assertFalse(m.is_blocking_ad(w,'qa.vpn'))
    def test_inline_close_button_is_still_pressed(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('MainActivity',[control('Connect',box=(400,400,600,600)),
                                 control('Advertisement',box=(0,900,1000,1000)),
                                 control('Close',box=(950,900,990,940))])
        with patch.object(m,'time',clock):self.assertTrue(c.service_dialog(w,ads=True))
        self.assertIn(('tap',0,'closed',970,920),d.trace)

    def test_target_app_crash_is_closed_then_fails_attempt(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('Application Error: qa.vpn',[
            control('QA VPN keeps stopping'),control('Close app','android:id/aerr_close')],package='')
        with patch.object(m,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'assigned app crashed'):c.wait(100,dialogs=True)
        self.assertTrue(any(row[0]=='tap' for row in d.trace))
        self.assertEqual(c.target_crash,'/Application Error: qa.vpn')
        self.assertFalse(any(row[0]=='log' and 'completed successfully' in row[2] for row in d.trace))
    def test_bluetooth_crash_does_not_fail_target_attempt(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        w=window('Application Error: com.android.bluetooth',[
            control('Bluetooth keeps stopping'),control('Close app','android:id/aerr_close')],package='')
        with patch.object(m,'time',clock):c.close_crash_dialog(w);c.check_target_crash()
        self.assertIsNone(c.target_crash)
    def test_target_crash_backoff_remains_retryable(self):
        clock=Clock();d=FakeDevice(clock)
        def fail(cycle):
            cycle.target_crash='/Application Error: qa.vpn'
            cycle.check_target_crash()
        with patch.object(m,'Device',return_value=d),patch.object(m,'time',clock),patch.object(m.Cycle,'run',fail):
            with self.assertRaises(SystemExit) as failure:m.main()
        self.assertEqual(failure.exception.code,75)
        self.assertEqual(clock.now,m.FAILED_CYCLE_BACKOFF_SECONDS)

    def test_missing_connect_does_not_tap_physical_screen_center(self):
        clock=Clock();d=FakeDevice(clock);d.state='main';c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('MainActivity',[])
        with patch.object(m,'time',clock),patch.object(m,'CONNECT_POSITION',None),patch.object(m,'POST_ACTION_QUIET_SECONDS',0):
            with self.assertRaisesRegex(RuntimeError,'No unique Connect control'):c.connect(window('MainActivity',[]))
        self.assertFalse(any(row[0]=='tap' for row in d.trace))
        self.assertFalse(any(row[0]=='adb' and row[2]==['shell','wm','size'] for row in d.trace))
    def test_observed_connect_uses_its_bounds_not_screen_center(self):
        clock=Clock();d=FakeDevice(clock);d.state='main';c=m.Cycle(d,'qa.vpn')
        with patch.object(m,'time',clock):
            c.connect(window('MainActivity',[control('Connect',box=(100,300,700,800))]))
        self.assertIn(('tap',0,'main',400,550),d.trace)

    def test_explicit_connect_position_allows_unobstructed_inline_banner(self):
        w=window('MainActivity',[control('Advertisement',box=(0,900,1000,1000))])
        with patch.object(m,'CONNECT_POSITION',(500,500)):
            self.assertFalse(m.is_blocking_ad(w,'qa.vpn'))

    def test_stale_permission_returns_home_and_reopens_assigned_app(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn');d.state='notification'
        original=d.inspect_window
        def observe(**kwargs):
            if d.state=='home':return window('.Home',[],package='com.android.launcher3')
            return original(**kwargs)
        def tap(x,y):
            d.trace.append(('tap',clock.now,d.state,x,y))
            if d.state=='notification':d.state='home'
        def reopen():d.trace.append(('reopen',clock.now));d.state='main'
        d.inspect_window=observe;d.tap=tap;d.app_open=reopen
        with patch.object(m,'time',clock):ready=c.prepare_screen(30)
        self.assertEqual(ready['package'],'qa.vpn')
        self.assertEqual(len([r for r in d.trace if r[0]=='reopen']),1)
        self.assertEqual(len([r for r in d.trace if r[0]=='adb' and 'android.intent.category.HOME' in r[2]]),1)
    def test_home_recovery_never_reopens_from_unrelated_app(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('.Settings',[],package='other.app')
        with patch.object(m,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'unobstructed'):c.prepare_screen(6)
        self.assertFalse(any(r[0]=='open' for r in d.trace))
    def test_home_recovery_attempts_only_once_per_prepare(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('com.android.launcher3.Home',[],package='com.android.launcher3')
        d.app_open=lambda:d.trace.append(('reopen',clock.now))
        with patch.object(m,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'unobstructed'):c.prepare_screen(8)
        self.assertEqual(len([r for r in d.trace if r[0]=='reopen']),1)
    def test_other_activity_in_launcher_package_is_not_home(self):
        clock=Clock();d=FakeDevice(clock);c=m.Cycle(d,'qa.vpn')
        d.inspect_window=lambda **kwargs:window('.Preferences',[],package='com.android.launcher3')
        with patch.object(m,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'unobstructed'):c.prepare_screen(6)
        self.assertFalse(any(r[0]=='open' for r in d.trace))

if __name__=='__main__':unittest.main(verbosity=2)
