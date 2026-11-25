"""Real Android13 W-schema regressions: close outside the D-root and nav metadata."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'qa'))
from shared_policy import window_snapshot
import script_runtime

XML = '''<displays><display id="0">
<window id="3970" layer="1" type="TYPE_SYSTEM" title="Navigation bar" bounds="[0,334][200,356]" active="false" focused="false">
 <node class="android.widget.FrameLayout" package="com.android.systemui" bounds="[0,334][200,356]" enabled="true">
  <node resource-id="com.android.systemui:id/back" content-desc="Back" class="android.widget.ImageView" package="com.android.systemui" bounds="[24,334][60,356]" enabled="true" clickable="true"/>
 </node>
</window>
<window id="3965" layer="0" type="TYPE_APPLICATION" title="V2 VPN" bounds="[0,0][200,356]" active="true" focused="true">
 <node class="android.widget.FrameLayout" package="v2.vpn.fast" bounds="[0,0][200,356]" enabled="true">
  <node class="android.webkit.WebView" package="v2.vpn.fast" bounds="[0,0][200,356]" enabled="true" clickable="true"/>
  <node text="Close" class="android.widget.Button" package="v2.vpn.fast" bounds="[183,4][196,17]" enabled="true" clickable="true"/>
 </node>
</window></display></displays>'''


class WindowTreeTests(unittest.TestCase):
    def test_real_schema_exposes_close_without_mixing_navigation(self):
        result=window_snapshot(XML)
        close=[n for n in result['controls'] if n['text']=='Close']
        self.assertEqual(len(close),1)
        self.assertEqual(close[0]['bounds'],[183,4,196,17])
        self.assertEqual(close[0]['window_id'],3965)
        self.assertEqual(close[0]['display_id'],0)
        self.assertFalse(any(n['resource_id'].endswith(':id/back') for n in result['controls']))
        back=[n for n in result['navigation_controls'] if n['resource_id'].endswith(':id/back')][0]
        self.assertEqual(back['window_title'],'Navigation bar')
        self.assertEqual(back['window_type'],'TYPE_SYSTEM')
        self.assertEqual(back['window_bounds'],[0,334,200,356])
        self.assertTrue(back['enabled'] and back['clickable'])

    def test_background_window_is_available_but_not_a_foreground_action(self):
        other='<window id="90" layer="-1" type="TYPE_APPLICATION" title="Old" active="false" focused="false"><node text="Close old" bounds="[10,10][20,20]" enabled="true" clickable="true"/></window>'
        result=window_snapshot(XML.replace('</display>',other+'</display>'))
        self.assertNotIn('Close old',[c['text'] for c in result['controls']])
        self.assertEqual(result['windows'][-1]['controls'][0]['text'],'Close old')

    def test_only_selected_display_contributes_actions(self):
        second='<display id="1"><window id="91" active="true"><node text="Other Close" bounds="[1,1][9,9]" enabled="true"/></window></display>'
        source=XML.replace('</displays>',second+'</displays>')
        self.assertFalse(any(c['text']=='Other Close' for c in window_snapshot(source,0)['controls']))
        self.assertEqual([c['text'] for c in window_snapshot(source,1)['controls']],['Other Close'])
        with self.assertRaisesRegex(ValueError,'selected Android display'):
            window_snapshot(source,2)

    def test_duplicate_display_rejected(self):
        with self.assertRaisesRegex(ValueError,'one hierarchy'):
            window_snapshot(XML.replace('</displays>','<display id="0"/></displays>'))

    def test_invisible_controls_never_become_actions(self):
        result=window_snapshot(XML.replace('text="Close"','text="Close" visible-to-user="false"'))
        self.assertFalse(any(c['text']=='Close' for c in result['controls']))

    def test_nested_window_nodes_not_duplicated_in_parent(self):
        nested='<window id="92" layer="2" active="true"><node text="Child Close" bounds="[1,1][5,5]" enabled="true"/></window>'
        source=XML.replace('</node>\n</window></display>',nested+'</node>\n</window></display>')
        result=window_snapshot(source)
        self.assertEqual(sum(c['text']=='Child Close' for c in result['controls']),1)

    def stock_window(self, content):
        return ('<displays><display id="0"><window id="9" type="TYPE_APPLICATION" '
                'bounds="[0,0][100,100]" active="true" focused="true">' + content
                + '</window></display></displays>')

    def stock_node(self, package="com.example.vpn"):
        return ('<node package="' + package + '" bounds="[0,0][100,100]" '
                'enabled="true"><node text="Connect" package="' + package + '" '
                'bounds="[10,10][30,30]" enabled="true"/></node>')

    def test_stock_hierarchy_wrapper_proves_same_window_root_and_preserves_controls(self):
        node = self.stock_node()
        wrapped = window_snapshot(self.stock_window('<hierarchy rotation="0">' + node + '</hierarchy>'))
        direct = window_snapshot(self.stock_window(node))
        self.assertEqual(wrapped, direct)
        self.assertTrue(wrapped['windows'][0]['root_node_present_in_XML'])
        self.assertEqual(wrapped['windows'][0]['root_package'], 'com.example.vpn')
        self.assertEqual(len(wrapped['controls']), 2)

    def test_stock_null_root_remains_missing(self):
        for content in ('', '<hierarchy rotation="0"/>'):
            item = window_snapshot(self.stock_window(content))['windows'][0]
            self.assertFalse(item['root_node_present_in_XML'])
            self.assertEqual(item['root_package'], '')

    def test_mixed_and_duplicate_root_encodings_cannot_claim_owner(self):
        node = self.stock_node()
        for content in (node + '<hierarchy>' + node + '</hierarchy>',
                        '<hierarchy>' + node + node + '</hierarchy>',
                        '<hierarchy>' + node + '</hierarchy><hierarchy/>', node + node):
            with self.subTest(content=content):
                item = window_snapshot(self.stock_window(content))['windows'][0]
                self.assertTrue(item['root_node_present_in_XML'])
                self.assertEqual(item['root_package'], '')

    def test_child_window_root_is_not_borrowed_by_parent(self):
        nested = ('<window id="10" type="TYPE_APPLICATION" active="false" focused="false">'
                  '<hierarchy>' + self.stock_node() + '</hierarchy></window>')
        result = window_snapshot(self.stock_window(nested))
        self.assertFalse(result['windows'][0]['root_node_present_in_XML'])
        self.assertEqual(result['windows'][0]['controls'], [])
        self.assertTrue(result['windows'][1]['root_node_present_in_XML'])
        self.assertEqual(result['windows'][1]['root_package'], 'com.example.vpn')

    def test_unrecognized_descendant_wrapper_cannot_claim_root(self):
        content = '<other><hierarchy>' + self.stock_node() + '</hierarchy></other>'
        item = window_snapshot(self.stock_window(content))['windows'][0]
        self.assertFalse(item['root_node_present_in_XML'])
        self.assertEqual(item['root_package'], '')
        self.assertEqual(len(item['controls']), 2)

    def test_legacy_hierarchy_shape_kept(self):
        result=window_snapshot('<hierarchy><node text="Allow" bounds="[1,1][5,5]" enabled="true"/></hierarchy>')
        self.assertEqual(result['controls'][0]['text'],'Allow')
        self.assertEqual(result['navigation_controls'],[])

    def test_sdk_old_arguments_unchanged_new_mode_explicit(self):
        device=script_runtime.Device()
        with patch.object(script_runtime,'request',return_value={}) as request:
            device.inspect_window();self.assertEqual(request.call_args.args,('inspect-window',[]))
            device.inspect_window(priority=0);self.assertEqual(request.call_args.args,('inspect-window',[None,0]))
            device.inspect_window(windows=True);self.assertEqual(request.call_args.args,('inspect-window',[None,None,True]))
        with self.assertRaises(ValueError):device.inspect_window(windows=1)


if __name__=='__main__':unittest.main()
