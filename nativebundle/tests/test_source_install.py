import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('public_bundle_apply',ROOT/'apply.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class SourceInstall(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.up=self.root/'up';self.up.mkdir();self.bundle=self.root/'bundle';(self.bundle/'source').mkdir(parents=True)
        self.git('init','-q');(self.up/'old.py').write_text('old\n')
        self.git('add','old.py');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
        base=self.git('rev-parse','HEAD').strip();self.base_patch=patch.object(m,'BASE',base);self.base_patch.start()
        rows=[]
        for name,data,before in [('old.py',b'updated\n',m.sha(b'old\n')),('new.py',b'new\n',None)]:
            (self.bundle/'source'/name).write_bytes(data)
            rows.append({'path':name,'sha256':m.sha(data),'bytes':len(data),'mode':'0644','before_sha256':[before]})
        value={'schema':m.SCHEMA,'upstream_revision':base,'files':rows}
        self.manifest=self.bundle/'manifest.json';self.manifest.write_text(json.dumps(value))
        self.pin=m.sha(self.manifest.read_bytes())

    def tearDown(self):self.base_patch.stop();self.temp.cleanup()

    def git(self,*args):return subprocess.check_output(['git','-C',str(self.up),*args],text=True,stderr=subprocess.DEVNULL)

    def test_check_then_apply_then_idempotent(self):
        self.assertEqual(m.apply(self.up,self.bundle,self.pin,check=True)['changes'],2)
        self.assertFalse((self.up/'new.py').exists())
        self.assertEqual(m.apply(self.up,self.bundle,self.pin)['changes'],2)
        self.assertEqual(m.apply(self.up,self.bundle,self.pin)['changes'],0)

    def test_wrong_manifest_no_mutation(self):
        with self.assertRaisesRegex(RuntimeError,'manifest_pin_changed'):m.apply(self.up,self.bundle,'0'*64)
        self.assertEqual((self.up/'old.py').read_text(),'old\n');self.assertFalse((self.up/'new.py').exists())

    def test_unknown_tracked_edit_no_mutation(self):
        (self.up/'old.py').write_text('operator edit\n')
        with self.assertRaisesRegex(RuntimeError,'unrecognized_source'):m.apply(self.up,self.bundle,self.pin)
        self.assertFalse((self.up/'new.py').exists())

    def test_foreign_untracked_collision_no_mutation(self):
        (self.up/'new.py').write_text('foreign\n')
        with self.assertRaisesRegex(RuntimeError,'unrecognized_source'):m.apply(self.up,self.bundle,self.pin)
        self.assertEqual((self.up/'old.py').read_text(),'old\n')

    def test_target_symlink_rejected(self):
        external=self.root/'external';external.write_text('old\n')
        (self.up/'old.py').unlink();(self.up/'old.py').symlink_to(external)
        with self.assertRaisesRegex(RuntimeError,'untrusted_file'):m.apply(self.up,self.bundle,self.pin)
        self.assertEqual(external.read_text(),'old\n')

    def test_publication_interruption_resumes_without_overwrite_of_third_state(self):
        real=m.atomic
        def fail_after(path,data,mode):
            real(path,data,mode)
            if path==self.up/'old.py':raise OSError('simulated interruption after publication')
        with patch.object(m,'atomic',side_effect=fail_after):
            with self.assertRaisesRegex(OSError,'simulated'):m.apply(self.up,self.bundle,self.pin)
        self.assertEqual((self.up/'old.py').read_bytes(),b'updated\n')
        self.assertEqual(m.apply(self.up,self.bundle,self.pin)['changes'],1)
        self.assertEqual((self.up/'new.py').read_bytes(),b'new\n')

    def test_undeclared_payload_rejected(self):
        (self.bundle/'source/extra.py').write_text('unexpected\n')
        with self.assertRaisesRegex(RuntimeError,'undeclared_source_payload'):m.apply(self.up,self.bundle,self.pin)
        self.assertEqual((self.up/'old.py').read_text(),'old\n')


if __name__=='__main__':unittest.main()
