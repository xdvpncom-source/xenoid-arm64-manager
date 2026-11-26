"""Actual Android exit-info timestamp parsing under localized decimal digits."""
import ast
import copy
from datetime import datetime, timezone
from pathlib import Path
import re
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1]/'nativebundle/source/src/xenoid/backend.py'
TREE = ast.parse(SOURCE.read_text())
OWNER = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == 'RuntimeManager')
NAMES = {'_parse_exit_info', '_parse_exit_timestamp', '_microg_exit_history'}
METHODS = [copy.deepcopy(node) for node in OWNER.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
for method in METHODS:method.decorator_list = []
NAMESPACE = {'datetime': datetime, 'timezone': timezone, 're': re,
             'time': types.SimpleNamespace(monotonic=lambda: 0)}
MODULE = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*METHODS], type_ignores=[])
exec(compile(ast.fix_missing_locations(MODULE),str(SOURCE),'exec'),NAMESPACE)
CONSTANTS = {node.targets[0].id: ast.literal_eval(node.value) for node in OWNER.body
             if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id.startswith('_EXIT_')}


def localized(value, digits):
    return value.translate(str.maketrans('0123456789',digits))


class TimestampLocaleTests(unittest.TestCase):
    def test_actual_arabic_timestamp_matches_ascii_and_timezone(self):
        expected=datetime(2026,11,26,16,29,27,tzinfo=timezone.utc).timestamp()
        for digits in ('0123456789','٠١٢٣٤٥٦٧٨٩','۰۱۲۳۴۵۶۷۸۹'):
            with self.subTest(digits=digits):
                value=localized('2026-11-26 18:29:27',digits)
                self.assertEqual(NAMESPACE['_parse_exit_timestamp'](value,'+0200'),expected)

    def test_other_unicode_decimal_scripts_and_existing_formats(self):
        for digits in ('०१२३४५६७८९','๐๑๒๓๔๕๖๗๘๙','０１２３４５６７８９'):
            for value in ('2026-11-26 18:29:27','11/26/26 18:29:27','11/26/2026 18:29:27'):
                with self.subTest(digits=digits,value=value):
                    self.assertEqual(NAMESPACE['_parse_exit_timestamp'](localized(value,digits),'-0330'),
                                     NAMESPACE['_parse_exit_timestamp'](value,'-0330'))

    def test_invalid_dates_times_and_nondecimal_numerals_still_reject(self):
        values=('2026-02-30 18:29:27','2026-13-26 18:29:27','2026-11-26 25:29:27',
                '2026-11-26 18:61:27','2026-11-26T18:29:27','garbage')
        for value in values:
            for digits in ('0123456789','٠١٢٣٤٥٦٧٨٩','۰۱۲۳۴۵۶۷۸۹'):
                with self.subTest(value=value,digits=digits):
                    self.assertIsNone(NAMESPACE['_parse_exit_timestamp'](localized(value,digits),'+0200'))
        self.assertIsNone(NAMESPACE['_parse_exit_timestamp']('²⁰²⁶-¹¹-²⁶ ¹⁸:²⁹:²⁷','+0200'))

    def history(self, entries, digits='٠١٢٣٤٥٦٧٨٩'):
        now=datetime(2026,11,26,16,40,tzinfo=timezone.utc).timestamp()
        lines=['package: com.android.vending']
        for offset,reason in entries:
            date=datetime.fromtimestamp(now+offset+7200,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            lines += ['  ApplicationExitInfo #0:', '    timestamp='+localized(date,digits)+' pid=123',
                      '    process=com.android.vending reason='+str(reason)+' (reason)']
        output={('dumpsys','activity','exit-info'):'\n'.join(lines),('getconf','CLK_TCK'):'100',
                ('cat','/proc/uptime'):'10000.0 20000.0',('date','+%s'):str(int(now)),('date','+%z'):'+0200'}
        model=types.SimpleNamespace(**CONSTANTS,_adb_shell_text=lambda args,**kw:output[tuple(args)],
                    _microg_process_identity=lambda name:(123,500000),
                    _parse_exit_info=NAMESPACE['_parse_exit_info'],
                    _parse_exit_timestamp=NAMESPACE['_parse_exit_timestamp'])
        return NAMESPACE['_microg_exit_history'](model,{'com.android.vending','com.google.android.gms'},100)

    def test_localized_current_boot_anr_still_blocks(self):
        result=self.history([(-100,6)])
        self.assertFalse(result['ok']);self.assertEqual(result['detail'],'com.android.vending:ANR')

    def test_localized_older_boot_anr_remains_filtered(self):
        self.assertTrue(self.history([(-6000,6)])['ok'])

    def test_localized_recent_crash_loop_blocks_but_old_crashes_do_not(self):
        result=self.history([(-100,4),(-90,4)],'۰۱۲۳۴۵۶۷۸۹')
        self.assertFalse(result['ok']);self.assertEqual(result['detail'],'com.android.vending:crashLoop')
        self.assertTrue(self.history([(-1000,4),(-900,4)])['ok'])

    def test_localized_future_exit_remains_unavailable(self):
        result=self.history([(5,1)])
        self.assertFalse(result['ok']);self.assertEqual(result['detail'],'exitInfoUnavailable')

    def test_ascii_and_localized_history_filters_are_identical(self):
        for entries in ([(-100,1)],[(-100,6)],[(-6000,6)],[(-90,4),(-80,4)],[(5,1)]):
            expected=self.history(entries,'0123456789')
            for digits in ('٠١٢٣٤٥٦٧٨٩','۰۱۲۳۴۵۶۷۸۹'):
                with self.subTest(entries=entries,digits=digits):self.assertEqual(self.history(entries,digits),expected)


if __name__=='__main__':unittest.main()
