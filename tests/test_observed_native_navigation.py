"""Real Broker/Cycle contracts with local getter and native-daemon collaborators.

No Android process, socket connection, ADB executable, device or pointer event is
used. Synthetic XML and coordinates are local test fixtures.
"""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa'))
import script_host as host
import qa_ui_reader as ui_reader
sys.modules['xenoid_device'] = types.SimpleNamespace(Device=object)
spec = importlib.util.spec_from_file_location('guarded_ad_cycle', ROOT / 'qa/vpn-cycle.py')
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)

FOCUS = ('WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n'
         '  Display: mDisplayId=0 (organized)\n'
         '  mCurrentFocus=Window{abc123 u0 v2.vpn.fast/com.google.android.gms.ads.AdActivity}\n'
         '  DisplayFrames w=100 h=100 r=0\n')
NAV = ('<window id="8" layer="10" type="TYPE_SYSTEM" title="Navigation bar" '
       'bounds="[0,90][100,100]" active="false" focused="false">'
       '<node resource-id="com.android.systemui:id/back" package="com.android.systemui" '
       'class="android.widget.ImageView" bounds="[0,90][20,100]" enabled="true" clickable="true"/>'
       '</window>')


def xml(extra=''):
    return '<displays><display id="0">' + NAV + extra + '</display></displays>'


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class NativeNavigation(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.host_time = patch.object(host, 'time', self.clock)
        self.cycle_time = patch.object(cycle, 'time', self.clock)
        self.host_time.start()
        self.cycle_time.start()
        self.addCleanup(self.host_time.stop)
        self.addCleanup(self.cycle_time.stop)
        self.logs, self.injected = [], []
        self.current_focus = FOCUS
        self.hierarchy = xml()
        self.read_seconds = 0
        self.setup_seconds = 0
        self.mapping_seconds = 0
        self.mapping = {'display_id': 0, 'orientation': 0, 'logical_size': [100, 100]}
        self.operation = threading.Lock()
        self.owner_depth = 0
        self.nav_visible = True
        self.nav_above = ''
        self.nav_config = 'NOT_FOCUSABLE | TRUSTED_OVERLAY | WATCH_OUTSIDE_TOUCH | SLIPPERY'
        self.nav_uid = 10066
        self.navigation_seconds = 0
        self.broker = host.Broker(None, lambda *row: self.logs.append(row), config={'command_timeout': 0})
        self.daemon = Mock()
        self.daemon.tap.side_effect = self.inject
        self.manager = types.SimpleNamespace(daemon_client=lambda **_: self.daemon)

        @contextlib.contextmanager
        def owned():
            with self.operation:
                self.owner_depth += 1
                try:
                    yield self.manager
                finally:
                    self.owner_depth -= 1

        self.broker.owned_manager = owned
        self.broker.adb = self.adb
        self.broker.ui_hierarchy = self.read
        self.broker.ensure_native_interaction = self.native_state
        self.broker.native_display_exec = self.final_focus
        self.reader = Mock(closed=False, started=True)
        self.reader.pipe.closed = False
        self.reader.pipe.process.poll.return_value = None
        self.reader.navigation.side_effect = self.fresh_navigation
        self.broker._ui_reader = self.reader
        self.transform = patch.object(host, 'native_touch_point', side_effect=lambda _, __, x, y: (x, y, self.mapping))
        self.transform.start()
        self.addCleanup(self.transform.stop)

    def adb(self, args, **_):
        self.assertIn(args, [self.broker.focus_command(), self.broker.focus_command(0)])
        self.clock.now += self.setup_seconds
        self.setup_seconds = 0
        return self.current_focus

    def read(self, *_args, **_kwargs):
        self.clock.now += self.read_seconds
        return self.hierarchy

    def native_state(self, manager, deadline, **_):
        self.assertIs(manager, self.manager)
        self.assertEqual(self.owner_depth, 1)
        self.clock.now += self.mapping_seconds
        return {'dump': 'local input fixture', 'profile': {}, 'mapping': self.mapping}

    def fresh_navigation(self, manager, display_id, deadline):
        self.assertEqual(self.owner_depth, 1)
        self.assertIs(manager, self.manager)
        self.clock.now += self.navigation_seconds
        data = host.shared_policy('window_snapshot', hierarchy=self.hierarchy, display_id=display_id)
        back = host.native_navigation_back({**data, 'stable': True})
        if back is None:
            raise ui_reader.DumpFailed('nav_back_unavailable')
        return {'schema': 'xenoid.nav-node-proof/v1', 'display_id': display_id,
                'window_id': back['window_id'], 'window_layer': back['window_layer'],
                'node_window_id': back['window_id'], 'window_id_scope': 'current_ui_automation_connection',
                'window_type': 'TYPE_SYSTEM', 'window_title': 'Navigation bar',
                'window_bounds': list(back['window_bounds']), 'bounds': list(back['bounds']),
                'package': 'com.android.systemui', 'resource_id': 'com.android.systemui:id/back',
                'visible': True, 'enabled': True, 'clickable': True, 'nodes_visited': 2,
                'cache_bypass': 'new_connection_setServiceInfo_root_target_refresh'}

    def routing_fixture(self):
        data = host.shared_policy('window_snapshot', hierarchy=self.hierarchy, display_id=0)
        back = host.native_navigation_back({**data, 'stable': True})
        frame = back['window_bounds'] if back else [0, 90, 100, 100]
        box = '[%d,%d][%d,%d]' % tuple(frame)
        visible = 'true' if self.nav_visible else 'false'
        windows = ('  Window #0 Window{abcdef u0 NavigationBar0}:\n'
                   '    mDisplayId=0 mSession=Session{ffeedd 1234:u0a10066}\n'
                   '    mOwnerUid=10066 package=com.android.systemui\n'
                   '    mAttrs={ty=NAVIGATION_BAR}\n'
                   '    mViewVisibility=0x0 mHaveFrame=true\n'
                   '    mHasSurface=true isReadyForDisplay()=true\n'
                   '    Frames: parent=[0,0][100,100] frame=' + box + '\n'
                   '    Surface: shown=' + visible + ' layer=0 alpha=1.0 rect=(0,0)\n'
                   '    mDrawState=HAS_DRAWN mLastHidden=false\n'
                   '    isOnScreen=' + visible + '\n    isVisible=' + visible + '\n')
        dispatcher = ('Input Dispatcher State:\n  DispatchEnabled: true\n  DispatchFrozen: false\n'
                      '  InputFilterEnabled: false\n  Pointer Capture Requested: false\n'
                      '  Display: 0\n    Windows:\n' + self.nav_above +
                      "      " + ('1' if self.nav_above else '0') + ": name='abcdef NavigationBar0', id=42, displayId=0, inputConfig=" + self.nav_config +
                      ', alpha=1.00, frame=' + box + ', globalScale=1.000000, applicationInfo.name=, '
                      'applicationInfo.token=<null>, touchableRegion=' + box +
                      ', ownerPid=1234, ownerUid=' + str(self.nav_uid) +
                      ', dispatchingTimeout=5000ms, hasToken=true, touchOcclusionMode=BLOCK_UNTRUSTED\n')
        return windows, dispatcher

    def final_focus(self, manager, args, **_):
        self.assertIs(manager, self.manager)
        self.assertEqual(self.owner_depth, 1)
        windows, dispatcher = self.routing_fixture()
        values = {('dumpsys', 'window', 'displays'): self.current_focus + '  mNavigationBar=Window{abcdef u0 NavigationBar0}\n',
                  ('dumpsys', 'window', 'windows'): windows, ('dumpsys', 'input'): dispatcher}
        return subprocess.CompletedProcess(args, 0, values[tuple(args)].encode(), b'')

    def inject(self, x, y):
        self.assertEqual(self.owner_depth, 1)
        self.injected.append((x, y))
        return {'ok': True}

    def snapshot(self):
        return self.broker.inspect_window(windows=True)

    def action(self, snapshot):
        return self.broker.handle_window(snapshot['native_navigation_observation'], 'back')

    def device(self):
        owner = self
        class Device:
            def settings(self):
                return {'android_user': 0}

            def inspect_window(self, **kwargs):
                return owner.broker.inspect_window(**kwargs)

            def click_window_action(self, window, action):
                return owner.broker.handle_window(window['observation'], action)

            def log(self, message):
                owner.logs.append(('cycle', message))

            def tap(self, *_):
                raise AssertionError('Navigation must use the guarded official action, not direct coordinates')

            def adb(self, *_):
                raise AssertionError('No additional script ADB probe or input is permitted')
        return Device()

    def test_late_partial_snapshot_credits_confirmed_focus_and_calls_real_official_tap(self):
        self.read_seconds = 57
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        window = c.observe()
        self.assertTrue(cycle.application_hierarchy_missing(window))
        self.assertEqual(window['controls'], [])
        self.assertEqual(window['focus_observation']['elapsed_seconds'], 57)
        self.assertTrue(c.service_missing_ad_close(window))
        self.assertEqual(self.injected, [(10, 95)])
        self.assertEqual(sum(c.missing_ad_close_backs.values()), 1)
        self.assertTrue(any(row[0] == 'action' and 'Xenoid native tap' in row[1] for row in self.logs))

    def test_reconnected_accessibility_id_can_change_with_same_fresh_node_and_real_route(self):
        fresh = self.reader.navigation.side_effect
        proofs = []
        def reconnected(*args):
            proof = fresh(*args)
            proof['window_id'] += 8
            proof['node_window_id'] = proof['window_id']
            proofs.append(proof)
            return proof
        self.reader.navigation.side_effect = reconnected
        snapshot = self.snapshot()
        old_id = self.broker._native_navigation_offer['window_id']
        self.action(snapshot)
        self.assertEqual(self.injected, [(10, 95)])
        self.assertNotEqual(proofs[0]['window_id'], old_id)
        self.assertEqual(proofs[0]['node_window_id'], proofs[0]['window_id'])
        self.assertTrue(any(row[0] == 'action' and 'Xenoid native tap' in row[1] for row in self.logs))

    def test_fresh_id_link_or_namespace_cannot_be_forged_or_omitted(self):
        fresh = self.reader.navigation.side_effect
        for update in ({'node_window_id': 9}, {'node_window_id': True}, {'window_id': -1, 'node_window_id': -1},
                       {'window_id': True, 'node_window_id': True}, {'window_id_scope': None},
                       {'window_id_scope': 'old_dump_connection'}):
            with self.subTest(update=update):
                self.reader.navigation.side_effect = lambda *args, update=update: {**fresh(*args), **update}
                snapshot = self.snapshot()
                result = self.action(snapshot)
                self.assertFalse(result['handled'])
                self.assertEqual(result['reason'], 'navigation_back_node_changed_or_unproven')
                self.assertEqual(self.injected, [])
        self.reader.navigation.side_effect = fresh

    def test_setup_or_rpc_time_before_first_verified_focus_is_not_grace(self):
        self.setup_seconds = 60
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        window = c.observe()
        self.assertEqual(c.last_read_stats['rpc_seconds'], 60)
        self.assertEqual(window['focus_observation']['elapsed_seconds'], 0)
        self.assertFalse(c.service_missing_ad_close(window))
        self.assertEqual(c.missing_close_reason, 'grace_not_elapsed')
        self.assertEqual(self.injected, [])

    def test_focus_changed_after_snapshot_is_rejected_before_pointer_input_and_consumed(self):
        window = self.snapshot()
        self.current_focus = FOCUS.replace('abc123', 'def456')
        result = self.action(window)
        self.assertFalse(result['handled'])
        self.assertEqual(result['reason'], 'navigation_focus_changed')
        self.assertFalse(self.action(window)['handled'])
        self.assertEqual(self.injected, [])

    def test_stale_offer_and_slow_mapping_reject_without_pointer_input(self):
        window = self.snapshot()
        self.clock.now += 6
        self.assertFalse(self.action(window)['handled'])
        window = self.snapshot()
        self.mapping_seconds = 6
        self.assertFalse(self.action(window)['handled'])
        self.assertEqual(self.injected, [])

    def test_new_read_generation_retires_old_offer_even_if_focus_is_same(self):
        old = self.snapshot()
        new = self.snapshot()
        self.assertFalse(self.action(old)['handled'])
        self.assertTrue(self.action(new)['handled'])
        self.assertEqual(self.injected, [(10, 95)])

    def test_latest_nav_only_provider_must_keep_same_back_not_old_same_bar_coordinates(self):
        old = self.snapshot()
        self.hierarchy = self.hierarchy.replace('[0,90][20,100]', '[70,90][90,100]')
        result = self.action(old)
        self.assertEqual(result['reason'], 'navigation_back_node_changed_or_unproven')
        self.assertEqual(self.injected, [])
        self.assertTrue(self.action(self.snapshot())['handled'])
        self.assertEqual(self.injected, [(80, 95)])

    def test_nav_removed_hidden_or_pass_through_rejects_without_official_input(self):
        old = self.snapshot()
        self.hierarchy = '<displays><display id="0"/></displays>'
        self.assertEqual(self.action(old)['reason'], 'navigation_fresh_back_unavailable')
        self.hierarchy = xml()
        self.nav_visible = False
        self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_routing_changed_or_unproven')
        self.nav_visible = True
        for flag in ('NOT_VISIBLE', 'NOT_TOUCHABLE', 'NO_INPUT_CHANNEL', 'DROP_INPUT', 'UNKNOWN_FLAG'):
            with self.subTest(flag=flag):
                self.nav_config = 'NOT_FOCUSABLE | ' + flag
                self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_routing_changed_or_unproven')
        self.assertEqual(self.injected, [])

    def test_higher_touchable_window_and_different_systemui_owner_reject(self):
        self.nav_above = ("      0: name='feed123 overlay', id=99, displayId=0, inputConfig=0x0, "
            'alpha=1.00, frame=[0,0][100,100], globalScale=1.000000, applicationInfo.name=, '
            'applicationInfo.token=<null>, touchableRegion=[0,0][100,100], ownerPid=99, ownerUid=10099, '
            'dispatchingTimeout=5000ms, hasToken=true, touchOcclusionMode=BLOCK_UNTRUSTED\n')
        self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_routing_changed_or_unproven')
        self.nav_above = ''
        self.nav_uid = 10099
        self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_routing_changed_or_unproven')
        self.assertEqual(self.injected, [])

    def test_rare_refresh_must_fit_offer_lease_and_never_start_another_reader(self):
        self.broker._ui_reader = None
        self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_reader_unavailable')
        self.broker._ui_reader = self.reader
        self.navigation_seconds = 6
        self.assertEqual(self.action(self.snapshot())['reason'], 'navigation_observation_stale')
        self.reader.start.assert_not_called()
        self.assertEqual(self.injected, [])

    def test_geometry_rotation_display_and_unproven_frames_reject_before_input(self):
        for replacement in ('w=120 h=100 r=0', 'w=100 h=100 r=2', ''):
            self.current_focus = FOCUS
            window = self.snapshot()
            self.current_focus = FOCUS.replace('w=100 h=100 r=0', replacement)
            with self.subTest(replacement=replacement):
                self.assertFalse(self.action(window)['handled'])
        self.current_focus = FOCUS
        window = self.snapshot()
        self.mapping = {**self.mapping, 'display_id': 1}
        self.assertFalse(self.action(window)['handled'])
        self.assertEqual(self.injected, [])

    def test_left_navigation_bar_in_rotated_display_uses_its_observed_bounds(self):
        self.current_focus = FOCUS.replace('w=100 h=100 r=0', 'w=100 h=200 r=3')
        self.mapping = {'display_id': 0, 'orientation': 3, 'logical_size': [100, 200]}
        self.hierarchy = xml().replace('[0,90][100,100]', '[0,0][10,200]').replace('[0,90][20,100]', '[0,100][10,120]')
        self.assertTrue(self.action(self.snapshot())['handled'])
        self.assertEqual(self.injected, [(5, 110)])

    def test_final_focus_getter_consumes_native_call_deadline_before_client_timeout(self):
        self.broker.config['command_timeout'] = 10
        calls = []
        self.manager.daemon_client = lambda **options: calls.append(options) or self.daemon
        original = self.final_focus
        def slow(*args, **kwargs):
            if args[1] == ['dumpsys', 'window', 'displays']:
                self.clock.now += 2
            return original(*args, **kwargs)
        self.broker.native_display_exec = slow
        self.mapping_seconds = 1
        self.assertTrue(self.action(self.snapshot())['handled'])
        self.assertEqual(calls, [{'timeout': 7}])

    def test_focus_transport_failure_preserves_exception_consumes_offer_and_releases_lock(self):
        window = self.snapshot()
        original = host.TransportUnavailable('original read-only focus failure')
        self.broker.native_display_exec = Mock(side_effect=original)
        with self.assertRaises(host.TransportUnavailable) as context:
            self.action(window)
        self.assertIs(context.exception, original)
        self.assertTrue(self.broker._native_navigation_lock.acquire(blocking=False))
        self.broker._native_navigation_lock.release()
        self.assertFalse(self.action(window)['handled'])
        self.assertEqual(self.injected, [])

    def test_geometry_change_inside_collection_yields_no_navigation_offer(self):
        self.broker.adb = Mock(side_effect=[FOCUS, FOCUS.replace('r=0', 'r=2')])
        window = self.snapshot()
        self.assertTrue(window['stable'])  # Backward-compatible focus meaning.
        self.assertFalse(window['focus_observation']['geometry_match'])
        self.assertNotIn('native_navigation_observation', window)
        self.assertEqual(self.injected, [])

    def test_wrong_nav_owner_or_ambiguous_navigation_never_gets_a_token(self):
        for nav in (NAV.replace('package="com.android.systemui"', 'package="other.app"'), NAV + NAV):
            self.hierarchy = '<displays><display id="0">' + nav + '</display></displays>'
            with self.subTest(nav=nav):
                self.assertNotIn('native_navigation_observation', self.snapshot())
        self.assertEqual(self.injected, [])

    def test_native_exception_is_original_and_consumed_token_cannot_reinject(self):
        window = self.snapshot()
        original = RuntimeError('original native failure')
        self.daemon.tap.side_effect = original
        with self.assertRaises(RuntimeError) as context:
            self.action(window)
        self.assertIs(context.exception, original)
        self.assertFalse(self.action(window)['handled'])
        self.assertEqual(self.daemon.tap.call_count, 1)

    def test_two_concurrent_uses_of_one_token_send_exactly_one_official_tap(self):
        window = self.snapshot()
        entered, release = threading.Event(), threading.Event()
        original = self.inject
        def held(x, y):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(x, y)
        self.daemon.tap.side_effect = held
        results = []
        first = threading.Thread(target=lambda: results.append(self.action(window)))
        second = threading.Thread(target=lambda: results.append(self.action(window)))
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive(), 'navigation/operation lock deadlock')
        self.assertEqual(sum(result['handled'] is True for result in results), 1)
        self.assertEqual(self.injected, [(10, 95)])

    def test_partial_hierarchy_is_diagnostic_not_unobstructed_readiness(self):
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        window = c.observe()
        c.last_ad_window = window
        c.ad_failure_evidence(window)
        line = next(row[1] for row in self.logs if row[0] == 'cycle' and row[1].startswith('Ad close evidence '))
        evidence = json.loads(line[len('Ad close evidence '):])
        self.assertEqual(evidence['reason'], 'application_hierarchy_unavailable')
        row = next(row[1] for row in self.logs if row[0] == 'cycle' and row[1].startswith('Ad window evidence '))
        metadata = json.loads(row[len('Ad window evidence '):])
        self.assertEqual(metadata['root_node_present_in_XML'], True)
        self.assertEqual(metadata['raw_node_count'], 1)
        self.assertEqual(self.injected, [])
        self.current_focus = FOCUS.replace('com.google.android.gms.ads.AdActivity', '.MainActivity')
        with patch.object(cycle, 'POLL_SECONDS', 1):
            with self.assertRaisesRegex(RuntimeError, 'unobstructed'):
                c.prepare_screen(3, ads=False)
        self.assertEqual(self.injected, [])

    def test_max_diagnostics_survive_actual_record_SQLite_without_JSON_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'state'
            host.prepare(folder)
            emitted = []
            device = self.device()
            def persist(message):
                emitted.append(message)
                with host.db(folder) as connection:
                    host.record(connection, 'local-fixture', 1, 'output', message)
            device.log = persist
            c = cycle.Cycle(device, 'v2.vpn.fast')
            window = c.observe()
            maximum = 2147483647
            metadata = {'window_id': maximum, 'window_layer': maximum, 'window_type': 'TYPE_' + 'A' * 59,
                'window_bounds': [0, 0, maximum, maximum], 'window_active': False, 'window_focused': False,
                'display_id': maximum, 'root_node_present_in_XML': True, 'root_package': 'a.' + 'b' * 253,
                'raw_node_count': maximum, 'controls': []}
            window['windows'] = [copy.deepcopy(metadata) for _ in range(16)]
            window['controls'] = [{'class_name': 'android.view.View', 'resource_id': '', 'text': '',
                'content_desc': '', 'bounds': [0, 0, maximum, maximum], 'enabled': True, 'clickable': False,
                'window_id': maximum, 'display_id': 0} for _ in range(128)]
            huge = 1.7976931348623157e308
            c.last_read_stats = {'rpc_seconds': huge, 'stable_focus': True, 'application_hierarchy_missing': True}
            c.missing_close_grace_seconds = huge
            c.last_navigation_result = {'handled': False, 'action': 'back', 'reason': 'a' * 96,
                'official_backend': 'xenoid_native', 'input_sent': False,
                'verification_seconds': huge, 'observation_age_seconds': huge}
            c.ad_failure_evidence(window)
            with host.db(folder) as connection:
                saved = [row['message'] for row in connection.execute('SELECT message FROM events ORDER BY seq')]
            self.assertEqual(saved, emitted, 'record() must not truncate any diagnostic event')
            prefixes = ('Ad close evidence ', 'Ad observation evidence ', 'Ad navigation evidence ',
                        'Ad window evidence ', 'Ad control evidence ')
            decoded = []
            for line in saved:
                self.assertLessEqual(len(line.encode()), 1600)
                prefix = next(prefix for prefix in prefixes if line.startswith(prefix))
                decoded.append(json.loads(line[len(prefix):]))
            header = decoded[0]
            windows = [line for line in saved if line.startswith('Ad window evidence ')]
            controls = [line for line in saved if line.startswith('Ad control evidence ')]
            self.assertEqual(header['windows_recorded'], len(windows))
            self.assertEqual(header['windows_recorded'] + header['windows_omitted'], 16)
            self.assertLessEqual(sum(len(line.encode()) + 1 for line in windows), 8192)
            self.assertLessEqual(sum(len(line.encode()) + 1 for line in controls), 24576)
            self.assertLess(sum(len(line.encode()) + 1 for line in saved), 65536)
            self.assertLessEqual(len(saved), 250)
            self.assertEqual(self.injected, [])

    def test_active_window_with_null_root_is_explicitly_partial(self):
        self.hierarchy = xml('<window id="9" layer="1" type="TYPE_APPLICATION" title="app" '
            'bounds="[0,0][100,90]" active="true" focused="true"/>')
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        window = c.observe()
        self.assertEqual(window['focus_observation']['hierarchy_state'], 'partial_application_root_missing')
        self.assertTrue(cycle.application_hierarchy_missing(window))
        self.assertFalse(window['windows'][1]['root_node_present_in_XML'])

    def stock_app_window(self, package='v2.vpn.fast'):
        return ('<window id="9" layer="1" type="TYPE_APPLICATION" title="app" '
                'bounds="[0,0][100,90]" active="true" focused="true">'
                '<hierarchy rotation="0"><node package="' + package + '" '
                'class="android.view.View" bounds="[0,0][100,90]" enabled="true">'
                '<node package="v2.vpn.fast" text="Connect" bounds="[20,20][40,40]" '
                'enabled="true" clickable="true"/></node></hierarchy></window>')

    def test_stock_wrapped_main_root_reaches_real_cycle_readiness_without_input(self):
        self.current_focus = FOCUS.replace('com.google.android.gms.ads.AdActivity', '.MainActivity')
        self.hierarchy = xml(self.stock_app_window())
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        with patch.object(cycle, 'POLL_SECONDS', 1):
            window = c.prepare_screen(4, ads=False)
        self.assertEqual(window['focus_observation']['hierarchy_state'], 'available')
        self.assertFalse(cycle.application_hierarchy_missing(window))
        self.assertTrue(window['windows'][1]['root_node_present_in_XML'])
        self.assertEqual(window['windows'][1]['root_package'], 'v2.vpn.fast')
        self.assertEqual(self.injected, [])

    def test_foreign_wrapped_root_with_target_connect_node_still_blocks_readiness(self):
        self.current_focus = FOCUS.replace('com.google.android.gms.ads.AdActivity', '.MainActivity')
        self.hierarchy = xml(self.stock_app_window('com.example.foreign'))
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        window = c.observe()
        self.assertEqual(window['focus_observation']['hierarchy_state'], 'partial_application_owner_mismatch')
        with patch.object(cycle, 'POLL_SECONDS', 1), self.assertRaisesRegex(RuntimeError, 'unobstructed'):
            c.prepare_screen(3, ads=False)
        self.assertEqual(self.injected, [])

    def test_late_back_gets_one_fresh_verification_allowance_and_success_requires_app_root(self):
        self.read_seconds = 179
        old_inject = self.inject
        def to_main(x, y):
            result = old_inject(x, y)
            self.current_focus = FOCUS.replace('abc123', 'def456').replace('com.google.android.gms.ads.AdActivity', '.MainActivity')
            self.hierarchy = xml('<window id="9" layer="1" type="TYPE_APPLICATION" title="app" '
                'bounds="[0,0][100,90]" active="true" focused="true">'
                '<node package="v2.vpn.fast" class="android.view.View" bounds="[0,0][100,90]" '
                'enabled="true" clickable="false"/></window>')
            self.read_seconds = 0
            return result
        self.daemon.tap.side_effect = to_main
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        with patch.object(cycle, 'AD_RECOVERY_VERIFY_SECONDS', 6):
            c.dismiss_ads()
        self.assertGreater(self.clock.now, 180)
        self.assertLess(self.clock.now, 185)
        self.assertFalse(cycle.application_hierarchy_missing(c.last_ad_window))
        self.assertEqual(self.injected, [(10, 95)])

    def test_late_back_with_persistently_partial_hierarchy_fails_without_repeating_or_extending(self):
        self.read_seconds = 179
        old_inject = self.inject
        def stays_partial(x, y):
            self.read_seconds = 0
            return old_inject(x, y)
        self.daemon.tap.side_effect = stays_partial
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        with patch.object(cycle, 'AD_RECOVERY_VERIFY_SECONDS', 6):
            with self.assertRaisesRegex(RuntimeError, 'usable close control'):
                c.dismiss_ads()
        self.assertEqual(self.clock.now, 185)
        self.assertEqual(self.injected, [(10, 95)])
        self.assertEqual(sum(c.missing_ad_close_backs.values()), 1)

    def test_changed_or_expired_cycle_snapshot_never_uses_old_offer(self):
        self.read_seconds = 16
        c = cycle.Cycle(self.device(), 'v2.vpn.fast')
        old = c.observe()
        self.clock.now += 6
        self.assertFalse(c.service_missing_ad_close(old))
        self.assertEqual(c.missing_close_reason, 'snapshot_not_latest_or_expired')
        self.read_seconds = 0
        self.current_focus = FOCUS.replace('abc123', 'fff456').replace('com.google.android.gms.ads.AdActivity', '.MainActivity')
        c.observe()
        self.assertFalse(c.service_missing_ad_close(old))
        self.assertEqual(self.injected, [])

    def test_native_token_dispatch_works_for_shared_and_manual_configs_without_legacy_flags(self):
        for options in ({'shared_app': True, 'window_actions': False},
                        {'qa_canary': True, 'window_actions': False, 'shared_app': False}):
            self.broker.config.update(options)
            window = self.broker.dispatch('inspect-window', [None, None, True])
            result = self.broker.dispatch('handle-window', [window['native_navigation_observation'], 'back'])
            self.assertTrue(result['handled'])
            self.assertEqual(result['official_backend'], 'xenoid_native')
        self.assertEqual(self.injected, [(10, 95), (10, 95)])

    def test_unknown_native_token_cannot_enter_legacy_back_key_path(self):
        self.broker.window_offer = ('legacy', {'back': ('Back', None)})
        self.broker.adb = Mock(side_effect=AssertionError('No ADB key path'))
        result = self.broker.dispatch('handle-window', ['xenoid-native-navigation/v1:unknown', 'back'])
        self.assertFalse(result['handled'])
        self.broker.adb.assert_not_called()
        self.assertEqual(self.injected, [])

    def test_existing_runtime_device_api_passes_native_token_without_api_change(self):
        import script_runtime
        device = script_runtime.Device()
        with patch.object(script_runtime, 'request', return_value={'handled': True}) as request:
            device.click_window_action({'observation': 'xenoid-native-navigation/v1:fixture'}, 'back')
        self.assertEqual(request.call_args.args,
                         ('handle-window', ['xenoid-native-navigation/v1:fixture', 'back']))


if __name__ == '__main__':
    unittest.main()
