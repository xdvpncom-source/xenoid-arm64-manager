"""Actual cold-profile and acceptance methods, with modeled read-only output."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import types
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'nativebundle/source/src/xenoid'
spec = importlib.util.spec_from_file_location('cold_profile_fixture', SOURCE / 'cold_profile.py')
profile = importlib.util.module_from_spec(spec); spec.loader.exec_module(profile)
PROFILE = json.loads((ROOT / 'nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json').read_text())
PROPS = profile.expected_properties(PROFILE)
tree = ast.parse((SOURCE / 'backend.py').read_text())
owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RuntimeManager')
method = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == 'cold_profile_status')
ns = {'Any':Any, 're':re, 'json':json, 'shlex':shlex, 'time':types.SimpleNamespace(),
      'normalize_cold_profile':profile.normalize_cold_profile,
      'expected_properties':profile.expected_properties, 'profile_digest':profile.profile_digest}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE / 'backend.py'), 'exec'), ns)

def output(props=None, type_keys=None):
    props = PROPS if props is None else props
    type_keys = props if type_keys is None else type_keys
    text = '\n'.join('['+k+']: [string]' for k in type_keys)
    text += '\n\n__PROP_VALUES__\n'+'\n'.join('['+k+']: ['+v+']' for k,v in props.items())
    text += ('\n\n__WM_SIZE__\nPhysical size: 1080x2400\n'
             '__WM_DENSITY__\nPhysical density: 420\n'
             '__MEMORY__\nMemTotal: 6291456 kB\n__CPU__\n8\n__INPUT__\n')
    text += json.dumps({'ok':True,'xMin':0,'xMax':1079,'yMin':0,'yMax':2399})
    text += ('\n__BLUETOOTH__\nsidecar-state\tvalid\nsidecar\t02:13:24:35:46:57\n'
             'persist\t02:13:24:35:46:57\ncontroller\t02:13:24:35:46:57\nready\t1\n')
    return text

class Model:
    def __init__(self, text=None, cost=0, result=None):
        self.cfg = types.SimpleNamespace(cold_profile=PROFILE)
        self.text = output() if text is None else text
        self.now = 0.; self.cost = cost; self.calls = []; self.result = result
    def docker_exec(self, args, timeout):
        self.calls.append((args,timeout)); self.now += self.cost
        return self.result if self.result is not None else {'ok':True,'stdout':self.text}
    def status(self, timeout=30):
        with patch.dict(ns, {'time':types.SimpleNamespace(monotonic=lambda:self.now)}):
            return ns['cold_profile_status'](self, timeout=timeout)

class ColdProfileObservation(unittest.TestCase):
    def test_real_191_contract_matches_two_constant_bulk_reads_without_writers(self):
        self.assertEqual(len(PROPS),191); self.assertTrue(all(PROPS.values()))
        model=Model(); result=model.status(timeout=10)
        self.assertTrue(result['ok']); self.assertEqual(result['checkedProperties'],191)
        self.assertEqual(len(model.calls),1); self.assertEqual(model.calls[0][1],10)
        command=model.calls[0][0][2]
        self.assertTrue(command.startswith('xenoid_profile_types=$(getprop -T) || exit $?; '))
        # Three existing, final Bluetooth coherence reads remain unchanged.
        self.assertEqual(len(re.findall(r'\bgetprop\b',command)),5)
        self.assertFalse(any(name in command for name in PROPS))
        self.assertNotIn('setprop',command); self.assertNotIn(' tap ',command)
        subprocess.run(['sh','-n'],input=command,text=True,capture_output=True,check=True)
    def test_missing_or_changed_property_fails_with_exact_mismatch(self):
        name=next(iter(PROPS)); missing=dict(PROPS); del missing[name]
        changed={**PROPS,name:'wrong'}
        for values in (missing,changed):
            with self.subTest(missing=name not in values):
                result=Model(output(values)).status()
                self.assertFalse(result['ok']); self.assertEqual(result['error'],'cold_profile_mismatch')
                self.assertEqual(result['mismatches'],[name])
        self.assertIn(name,Model(output(missing,type_keys=PROPS)).status()['mismatches'])
    def test_even_an_expected_empty_property_requires_presence(self):
        with patch.dict(ns, {'expected_properties':lambda unused:{'ro.test.empty':''}}):
            self.assertTrue(Model(output({'ro.test.empty':''})).status()['ok'])
            self.assertFalse(Model(output({})).status()['ok'])
    def test_duplicate_selected_or_unselected_and_malformed_lines_are_invalid(self):
        name=next(iter(PROPS)); row='['+name+']: ['+PROPS[name]+']\n'
        invalid=[row+output(), '[unrelated]: [one]\n[unrelated]: [two]\n'+output(),
                 output().replace('__PROP_VALUES__\n','__PROP_VALUES__\n'+row),
                 '[broken]: no closing bracket\n'+output(), 'noise\n'+output()]
        for text in invalid:
            with self.subTest(prefix=text[:30]):
                self.assertEqual(Model(text).status(),{'ok':False,'error':'cold_profile_observation_invalid'})
    def test_unrelated_values_are_not_returned_or_normalized(self):
        text='[unrelated.private]: [DO_NOT_RETURN [brackets]]\n'+output()
        result=Model(text).status(); self.assertFalse(result['ok'])
        self.assertNotIn('DO_NOT_RETURN',json.dumps(result))
        name=next(iter(PROPS)); changed={**PROPS,name:' '+PROPS[name]}
        self.assertIn(name,Model(output(changed)).status()['mismatches'])
    def test_guest_filter_excludes_realistic_unrelated_multiline_and_large_values(self):
        model=Model();model.status();prefix=model.calls[0][0][2].splitlines()[0]
        selected='\n'.join('['+k+']: ['+v+']'for k,v in PROPS.items())
        type_text='\n'.join('['+k+']: [string]'for k in PROPS)+'\n'
        unrelated=('[persist.sys.boot.reason.history]: [reboot,100\nshutdown,200\nboot,300]\n'
                   '[unrelated.url]: [https://example.invalid/?case=DO_NOT_TRANSPORT]\n')
        unrelated+=''.join('[unrelated.'+str(i)+']: ['+'x'*100+']\n'for i in range(800))
        payload=selected+'\n'+unrelated
        self.assertGreater(len(payload.encode()),65536)
        with tempfile.TemporaryDirectory()as folder:
            directory=Path(folder);properties=directory/'props.txt';properties.write_text(payload)
            types_file=directory/'types.txt';types_file.write_text(type_text)
            getprop=directory/'getprop'
            for exitcode in (0,7):
                getprop.write_text('#!'+sys.executable+'\nimport pathlib,sys\np='+repr(str(types_file))+' if "-T" in sys.argv else '+repr(str(properties))+'\nsys.stdout.write(pathlib.Path(p).read_text())\nsys.exit('+str(exitcode)+')\n');getprop.chmod(0o755)
                result=subprocess.run(['sh','-c',prefix],env={**os.environ,'PATH':str(directory)+os.pathsep+os.environ['PATH']},text=True,capture_output=True)
                if exitcode:
                    self.assertEqual(result.returncode,exitcode);self.assertEqual(result.stdout,'')
                else:
                    self.assertEqual(result.returncode,0);self.assertEqual(result.stdout.count('__PROP_VALUES__'),1)
                    self.assertEqual(len([line for line in result.stdout.splitlines()if line.startswith('[')]),382)
                    self.assertLess(len(result.stdout.encode()),65536)
                    self.assertNotIn('persist.sys.boot.reason.history',result.stdout)
                    self.assertNotIn('DO_NOT_TRANSPORT',result.stdout)
                    # Exercise the actual host parser on the filtered output.
                    tail='\n__WM_SIZE__\n'+output().split('\n__WM_SIZE__\n',1)[1]
                    self.assertTrue(Model(result.stdout+tail).status()['ok'])
    def test_forged_value_line_cannot_supply_an_actually_missing_property(self):
        model=Model();model.status();prefix=model.calls[0][0][2].splitlines()[0]
        missing={k:v for k,v in PROPS.items()if k!='ro.hardware'}
        selected='\n'.join('['+k+']: ['+v+']'for k,v in missing.items())
        values=selected+'\n[persist.sys.boot.reason.history]: [reboot\n[ro.hardware]: ['+PROPS['ro.hardware']+']\nboot]\n'
        metadata='\n'.join('['+k+']: [string]'for k in missing)+'\n[persist.sys.boot.reason.history]: [string]\n'
        with tempfile.TemporaryDirectory()as folder:
            directory=Path(folder);v=directory/'values';v.write_text(values);t=directory/'types';t.write_text(metadata)
            getprop=directory/'getprop';getprop.write_text('#!'+sys.executable+'\nimport pathlib,sys\np='+repr(str(t))+' if "-T" in sys.argv else '+repr(str(v))+'\nsys.stdout.write(pathlib.Path(p).read_text())\n');getprop.chmod(0o755)
            result=subprocess.run(['sh','-c',prefix],env={**os.environ,'PATH':str(directory)+os.pathsep+os.environ['PATH']},text=True,capture_output=True,check=True)
        tail='\n__WM_SIZE__\n'+output().split('\n__WM_SIZE__\n',1)[1]
        observed=Model(result.stdout+tail).status();self.assertFalse(observed['ok'])
        self.assertIn('ro.hardware',observed['mismatches'])
    def test_timeout_failed_and_invalid_remain_distinct_and_fail_closed(self):
        cases=[(Model(cost=10.001),'cold_profile_observation_timeout'),
               (Model(result={'ok':False,'error':'docker_exec_timeout'}),'cold_profile_observation_timeout'),
               (Model(result={'ok':False,'error':'unavailable'}),'cold_profile_observation_failed'),
               (Model('partial output'),'cold_profile_observation_invalid')]
        for model,error in cases:
            with self.subTest(error=error):self.assertEqual(model.status(timeout=10),{'ok':False,'error':error})
        self.assertTrue(Model(cost=9.9).status(timeout=10)['ok'])
    def test_budget_is_not_expanded_and_legacy_none_does_not_read(self):
        for request,expected in ((100,30),(1,1)):
            model=Model();model.status(timeout=request);self.assertEqual(model.calls[0][1],expected)
        model=Model();model.cfg.cold_profile=None
        self.assertEqual(model.status(),{'ok':True,'skipped':True});self.assertEqual(model.calls,[])
    def test_display_memory_CPU_input_and_Bluetooth_checks_remain_required(self):
        cases=[('Physical size: 1080x2400','Physical size: 720x1280','display.physicalSize'),
               ('Physical density: 420','Physical density: 560','display.physicalDensity'),
               ('Physical size: 1080x2400','Physical size: 1080x2400\nOverride size: 720x1280','display.overrideSize'),
               ('Physical density: 420','Physical density: 420\nOverride density: 560','display.overrideDensity'),
               ('MemTotal: 6291456 kB','MemTotal: 2097152 kB','memory.totalKiB'),
               ('__CPU__\n8','__CPU__\n1','cpu.reportedCount'),
               ('"xMax": 1079','"xMax": 1080','input.rawAxes'),
               ('sidecar-state\tvalid','sidecar-state\tinvalid','bluetooth.target'),
               ('controller\t02:13:24:35:46:57','controller\t02:AA:BB:CC:DD:EE','bluetooth.controllerAddress'),
               ('ready\t1','ready\t0','bluetooth.ready')]
        for before,after,expected in cases:
            with self.subTest(expected=expected):
                result=Model(output().replace(before,after)).status()
                self.assertFalse(result['ok']);self.assertIn(expected,result['mismatches'])

# Load the actual read-only acceptance implementation, replacing only its
# unrelated package import; this fixture never calls identity epoch lookup.
live_tree=ast.parse((SOURCE/'live_observe.py').read_text())
live_tree.body=[n for n in live_tree.body if not(isinstance(n,ast.ImportFrom)and n.module=='device_identity')]
live={'__name__':'cold_profile_live_fixture','container_epoch':lambda unused:(_ for _ in()).throw(AssertionError('unneeded identity lookup'))}
exec(compile(live_tree,str(SOURCE/'live_observe.py'),'exec'),live)

class LiveColdProfileErrors(unittest.TestCase):
    def observe(self, result):
        calls=[]
        def getter(*,timeout=30):calls.append(timeout);return result
        manager=types.SimpleNamespace(cfg=types.SimpleNamespace(cold_profile=PROFILE),cold_profile_status=getter)
        snapshot={'running':True,'containerId':'a'*64,'imageId':'sha256:'+'b'*64,
                  'runtime':{'bootSeedRequired':False,'networkMatches':True,'volumeMatches':True},
                  'storage':{'ok':True,'state':'committed','filesystemUuid':'11111111-2222-4333-8444-555555555555'},
                  'storageSentinel':{'ok':True},'identity':{'ok':True,'initialized':True,'phase':'applied'},
                  'adb':{'connected':True,'bootCompleted':True}}
        checks=live['_Checks']();live['LiveAcceptance']()._host_checks(manager,{},snapshot,{},live['_Budget'](None),checks)
        self.assertEqual(calls,[10.0]);return checks.values
    def test_getter_failures_keep_their_safe_specific_code(self):
        for code in ('cold_profile_observation_timeout','cold_profile_observation_failed','cold_profile_observation_invalid'):
            with self.subTest(code=code):
                checks=self.observe({'ok':False,'error':code})
                self.assertEqual(checks['coldProfile'],{'ok':False,'code':code})
                self.assertFalse(checks['identity']['ok'])
    def test_actual_mismatch_and_unknown_error_cannot_become_accepted(self):
        for error in ('cold_profile_mismatch','accepted','arbitrary secret detail',None,{},[]):
            checks=self.observe({'ok':False,'error':error})
            self.assertEqual(checks['coldProfile'],{'ok':False,'code':'cold_profile_mismatch'})
        self.assertEqual(self.observe({'ok':True})['coldProfile'],{'ok':True,'code':'accepted'})

if __name__=='__main__':unittest.main()
