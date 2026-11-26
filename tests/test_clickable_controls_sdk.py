"""Complete clickable inventories and fresh native selection over existing RPCs."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import script_runtime as sdk


def item(text='Close', **extra):
    return {'text':text,'content_desc':'Dismiss ad','resource_id':'qa:id/close','class_name':'android.widget.Button',
            'bounds':[10,20,50,60],'enabled':True,'clickable':True,'package':'qa.app','window_id':4,
            'display_id':0,'window_active':True,'window_focused':True,**extra}


def snapshot(nodes):
    return {'stable':True,'package':'qa.app','component':'qa.app/.AdActivity','display_id':0,
            'controls':nodes,'navigation_controls':[]}


class Tests(unittest.TestCase):
    def setUp(self): self.device=sdk.Device()

    def test_inventory_keeps_full_fields_disabled_rows_and_no_count_cap(self):
        nodes=[item(str(i),resource_id=f'qa:id/c{i}',enabled=i!=2)for i in range(350)]
        value=self.device.clickable_controls(snapshot(nodes))
        self.assertEqual(len(value),350);self.assertEqual(value[-1]['index'],350)
        self.assertFalse(value[2]['enabled'])
        self.assertEqual({k:value[0][k] for k in nodes[0]},nodes[0])

    def test_nonclickable_and_inactive_app_rows_excluded_but_navigation_retained(self):
        window=snapshot([item(),item('Label',clickable=False),item('Background',window_active=False,window_focused=False)])
        window['navigation_controls']=[item('Back',package='com.android.systemui',resource_id='system:id/back',window_active=False,window_focused=False)]
        self.assertEqual([x['text']for x in self.device.clickable_controls(window)],['Close','Back'])

    def test_live_inventory_uses_existing_inspection_rpc_and_sends_no_input(self):
        with patch.object(sdk,'request',return_value=snapshot([item()]))as request:
            self.assertEqual(self.device.clickable_controls()[0]['index'],1)
        request.assert_called_once_with('inspect-window',[None,None,True])

    def test_click_reinspects_exact_target_before_existing_native_tap_rpc(self):
        window=snapshot([item()]);choice=self.device.clickable_controls(window)[0]
        with patch.object(sdk,'request',side_effect=[copy.deepcopy(window),'sent'])as request:
            self.assertEqual(self.device.click_control(choice,window=window),'sent')
        self.assertEqual(request.call_args_list[0].args,('inspect-window',[None,None,True]))
        self.assertEqual(request.call_args_list[1].args,('tap',[30,40]))

    def test_changed_window_node_enabled_state_or_bounds_never_taps(self):
        old=snapshot([item()]);choice=self.device.clickable_controls(old)[0]
        altered=[snapshot([]),snapshot([item(enabled=False)]),snapshot([item(bounds=[20,20,60,60])]),
                 snapshot([item(window_id=8)]),{**old,'package':'other.app'}, {**old,'stable':False},
                 snapshot([item(window_active=False,window_focused=False)])]
        for fresh in altered:
            with self.subTest(fresh=fresh),patch.object(sdk,'request',return_value=fresh)as request:
                with self.assertRaises(RuntimeError):self.device.click_control(choice,window=old)
                self.assertEqual(request.call_count,1)

    def test_disabled_or_fabricated_choice_is_rejected_before_io(self):
        for choice in (item(enabled=False),item(resource_id='qa:id/fake')):
            with patch.object(sdk,'request')as request,self.assertRaises(ValueError):
                self.device.click_control(choice,window=snapshot([item()]))
            request.assert_not_called()


if __name__=='__main__':unittest.main()
