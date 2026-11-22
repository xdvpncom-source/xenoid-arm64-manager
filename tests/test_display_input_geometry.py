"""Generated display presets must reach every official input mapping layer."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h
import qa_input_ready as q

FIXTURE=Path(__file__).with_name('test_native_input_fixture.txt').read_text()
PROFILE={'schema':'dev.xenoid.fingerprint/v1','display':{'width':200,'height':356,'densityDpi':72},
         'input':{'name':'sec_touchscreen','x':{'minimum':0,'maximum':199},'y':{'minimum':0,'maximum':355}}}

def make_dump(orientation=0,physical_scaling=True):
    text=FIXTURE.replace('max=1439','max=199').replace('max=3119','max=355')
    if not physical_scaling:
        text=text.replace('DisplayWidth: 720px','DisplayWidth: 200px').replace('DisplayHeight: 1280px','DisplayHeight: 356px')
        text=text.replace('PhysicalWidth: 719px','PhysicalWidth: 199px').replace('PhysicalHeight: 1280px','PhysicalHeight: 355px')
        text=text.replace('0.2782','1.0000').replace('0.2781','1.0000')
    text=text.replace('InputDeviceOrientation: 0','InputDeviceOrientation: '+str(orientation))
    if orientation%2:text=text.replace('logicalSize=200x356','logicalSize=356x200')
    return text

def make_state(profile=None,dump=None):
    profile=copy.deepcopy(profile or PROFILE);dump=dump or make_dump()
    raw=json.dumps(profile).encode();sha=hashlib.sha256(raw).hexdigest()
    try:_,_,mapping=h.native_touch_point(dump,profile,None,None);ready=True
    except Exception:mapping=None;ready=False
    return {'profile':profile,'profile_sha256':sha,'dump':dump,'mapping':mapping,'ready':ready}

def evidence(state,wrong_axis=False):
    profile=state['profile'];x=profile['input']['x']['maximum'];y=profile['input']['y']['maximum']
    status={'ok':True,'schema':'dev.input/v2','name':'sec_touchscreen','xMin':0,'xMax':x,'yMin':0,'yMax':y,'driverLayer':True,'directTouch':True,'persistentDevice':True,'eventNode':'/dev/input/event3'}
    kernel='name: "sec_touchscreen"\n'+''.join(' '+axis+' : value 0, min 0, max '+str(high)+', fuzz 0\n' for axis,high in [('ABS_X',x-1 if wrong_axis else x),('ABS_Y',y),('ABS_MT_POSITION_X',x),('ABS_MT_POSITION_Y',y)])+'INPUT_PROP_DIRECT\n'
    fields={'STATUS':json.dumps(status),'PID':'20','START':'8142202','NODE':'/dev/input/event3','ALIAS':'/dev/input/.xenoid-reannounce-20-8142202-event3','UINPUT_FD':'3','SOCKET_FD':'4','SOCKET_INODE':'4831484','META':'60:604:21b0:d:43:0:1004:660','PROFILE_SHA':state['profile_sha256'],'SERVER_SHA':'b'*64,'CLIENT_SHA':'b'*64,'SYSFS_NAME':base64.b64encode(b'sec_touchscreen\n').decode(),'SYSFS_DEV':'13:67','PROPERTIES':'2','GETEVENT':base64.b64encode(kernel.encode()).decode()}
    return ''.join(k+'\t'+v+'\n'for k,v in fields.items())

class DisplayInputGeometryTests(unittest.TestCase):
    def setUp(self):
        global q
        q=sys.modules['qa_input_ready']
        self.manager=MagicMock();self.manager._owned_container_record.return_value=({'Id':'a'*64,'State':{'Running':True}},None)
        self.manager._userspace_runtime_epoch.return_value='epoch-1'
        self.b=h.Broker(self.manager,lambda *_:None,config={'expected_container_id':'a'*64})
        self.state=make_state();self.b.native_interaction_snapshot=MagicMock(side_effect=lambda *a,**kw:self.state)
        self.b.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,b'Physical size: 720x1280\nOverride size: 200x356\nPhysical density: 560\nOverride density: 72\n',b''))
        self.b.native_input_rootd=MagicMock(side_effect=lambda *a:evidence(self.state))
        self.b.ensure_native_interaction=MagicMock(return_value=self.state)
        self.manager.daemon_client.return_value.tap.side_effect=AssertionError('No probe may inject events')
        self.b.adb=MagicMock(side_effect=AssertionError('No framework input'))
    def verify(self):return self.b.verify_native_display_geometry(self.manager,PROFILE,None)
    def test_all_layers_verified_without_events_or_physical_size_equality(self):
        p=self.verify();self.assertTrue(p['ok']);self.assertFalse(p['events_injected']);self.assertEqual(len(p['mapped_points']),9)
        self.assertEqual(p['display']['logical_size'],[200,356]);self.assertEqual(p['raw_bounds'],{'x':[0,199],'y':[0,355]})
        self.assertEqual(p['event_node'],'/dev/input/event3');self.b.native_input_rootd.assert_called_once()
        self.manager.daemon_client.assert_not_called();self.b.adb.assert_not_called();self.b.ensure_native_interaction.assert_not_called()
    def test_all_rotations_map_nine_points(self):
        for orientation in range(4):
            with self.subTest(orientation=orientation):
                self.state=make_state(dump=make_dump(orientation,False));p=self.verify()
                self.assertEqual(p['display']['orientation'],orientation)
                self.assertEqual(p['display']['logical_size'],[356,200]if orientation%2 else[200,356])
                self.assertTrue(all(0<=v['raw'][0]<=199 and 0<=v['raw'][1]<=355 for v in p['mapped_points']))
    def test_mismatched_json_axis_rejected_before_any_probe(self):
        bad=copy.deepcopy(PROFILE);bad['input']['x']['maximum']=200
        with self.assertRaisesRegex(q.NativeInputBindingError,'axes disagree'):self.b.verify_native_display_geometry(self.manager,bad,None)
        self.b.native_input_rootd.assert_not_called();self.b.native_interaction_snapshot.assert_not_called()
    def test_single_pixel_and_boolean_geometry_rejected(self):
        for key,value in [('width',1),('height',1),('width',True)]:
            bad=copy.deepcopy(PROFILE);bad['display'][key]=value
            with self.assertRaises(q.NativeInputBindingError):self.b.verify_native_display_geometry(self.manager,bad,None)
    def test_kernel_axis_mismatch_rejected_even_on_ready_fastpath(self):
        self.b.native_input_rootd.side_effect=lambda *a:evidence(self.state,wrong_axis=True)
        with self.assertRaisesRegex(q.NativeInputBindingError,'Kernel raw axes'):self.verify()
    def test_native_profile_hash_mismatch_rejected(self):
        self.b.native_input_rootd.side_effect=lambda *a:evidence(self.state).replace(self.state['profile_sha256'],'f'*64)
        with self.assertRaisesRegex(q.NativeInputBindingError,'probe and generated profile'):self.verify()
    def test_inputreader_wrong_event_node_rejected(self):
        self.state=make_state(dump=make_dump().replace('Path: /dev/input/event3','Path: /dev/input/event9'))
        with self.assertRaisesRegex(q.NativeInputBindingError,'verified official event node'):self.verify()
    def test_wm_size_or_density_mismatch_rejected(self):
        for old,new in [(b'200x356',b'202x360'),(b'Override density: 72',b'Override density: 74')]:
            self.b.native_display_exec.return_value.stdout=b'Physical size: 720x1280\nOverride size: 200x356\nPhysical density: 560\nOverride density: 72\n'.replace(old,new)
            with self.assertRaisesRegex(q.NativeInputBindingError,'WindowManager metrics'):self.verify()
    def test_final_profile_swap_rejected(self):
        final=copy.deepcopy(self.state);final['profile']['display']['width']=202
        self.b.native_interaction_snapshot.side_effect=[self.state,final]
        with self.assertRaisesRegex(q.NativeInputBindingError,'generated profile changed'):self.verify()
    def test_userspace_epoch_change_rejected(self):
        self.manager._userspace_runtime_epoch.side_effect=['epoch-1','epoch-2']
        with self.assertRaisesRegex(q.NativeInputBindingError,'display changed'):self.verify()
    def test_wrong_orientation_logical_size_rejected(self):
        self.state=make_state(dump=make_dump(1,False).replace('logicalSize=356x200','logicalSize=200x356'))
        with self.assertRaisesRegex(q.NativeInputBindingError,'logical viewport'):self.verify()
    def test_nonrepresentable_corner_is_rejected(self):
        # Centre still maps but extreme coordinates exceed raw bounds.
        text=make_dump().replace('0.2782','0.3000').replace('0.0000\n            -0.0000','-20.0000\n            -0.0000',1)
        self.state=make_state(dump=text)
        self.assertTrue(self.state['ready'])
        with self.assertRaises(q.NativeInputBindingError):self.verify()
    def test_only_official_readiness_repair_is_used_when_needed(self):
        ready=self.state;not_ready=copy.deepcopy(ready);not_ready['ready']=False
        self.b.native_interaction_snapshot.side_effect=[not_ready,ready]
        self.b.ensure_native_interaction.return_value=ready
        self.verify();self.b.ensure_native_interaction.assert_called_once_with(self.manager,None,repair=True)
        self.manager.daemon_client.assert_not_called()

if __name__=='__main__':unittest.main()
