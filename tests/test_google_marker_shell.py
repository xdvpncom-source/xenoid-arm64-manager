"""Execute the exact guest marker shell against real private filesystem fixtures.

Portable adapters implement toybox/GNU stat/readlink/sync on the desktop; the
script, real file modes, links, content checks, unlink and directory fsync run.
Only the fixture path and expected owner numbers differ from Android root.
"""
import ast
import hashlib
import inspect
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h

SHIM=r'''#!PYTHON
import hashlib,os,stat,sys
from pathlib import Path
name=Path(sys.argv[0]).name
if name=='stat':
 assert sys.argv[1]=='-c';fmt=sys.argv[2];p=Path(sys.argv[3]);v=p.lstat()
 fields={'%u':str(v.st_uid),'%g':str(v.st_gid),'%a':oct(stat.S_IMODE(v.st_mode))[2:],'%h':str(v.st_nlink),'%s':str(v.st_size)}
 if os.environ.get('FAKE_FOREIGN_OWNER')==str(p):fields['%u']='987654'
 for key,value in fields.items():fmt=fmt.replace(key,value)
 print(fmt)
elif name=='readlink':
 assert sys.argv[1]=='-f';print(os.path.realpath(sys.argv[2]))
elif name=='sync':
 assert sys.argv[1]=='-f';fd=os.open(sys.argv[2],os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
 try:os.fsync(fd)
 finally:os.close(fd)
 with open(os.environ['FSYNC_LOG'],'a')as f:f.write(sys.argv[2]+'\n')
elif name=='sha256sum':print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+'  -')
else:raise SystemExit(99)
'''

class MarkerShellTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve();self.profile=self.root/'profile';self.profile.mkdir(mode=0o700);self.marker=self.profile/'gsf_android_id';self.marker.write_bytes(b'1234567890\n');self.marker.chmod(0o600)
        module=ast.parse(Path(h.__file__).read_text());owner=next(n for n in module.body if isinstance(n,ast.ClassDef)and n.name=='Broker');tree=next(n for n in owner.body if isinstance(n,ast.FunctionDef)and n.name=='retire_recorded_google_offline_marker')
        self.script=next(n.value.value for n in ast.walk(tree)if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='script'for t in n.targets))
        self.assertIn("'0:0:700'",self.script);self.assertIn("'0:0:600:1'",self.script)
        self.script=self.script.replace('profile=/data/local/tmp/xenoid-profile','profile='+shlex.quote(str(self.profile)))
        self.script=self.script.replace("'0:0:700'",repr(str(os.getuid())+':'+str(os.getgid())+':700')).replace("'0:0:600:1'",repr(str(os.getuid())+':'+str(os.getgid())+':600:1'))
        bin_dir=self.root/'bin';bin_dir.mkdir();shim=SHIM.replace('PYTHON',sys.executable,1)
        for name in ('stat','readlink','sync','sha256sum'):
            p=bin_dir/name;p.write_text(shim);p.chmod(0o755)
        self.log=self.root/'fsync.log';self.env={**os.environ,'PATH':str(bin_dir)+':'+os.environ.get('PATH',''),'FSYNC_LOG':str(self.log)}
        self.expected=hashlib.sha256(b'1234567890').hexdigest()
    def run_marker(self,operation='retire',allow=False,**options):
        return subprocess.run(['sh','-c',self.script,'marker-fixture',operation,self.expected,'1'if allow else'0'],capture_output=True,text=True,env={**self.env,**options},timeout=5)
    def test_exact_canonical_marker_is_retired_and_parent_synced(self):
        p=self.run_marker(allow=True);self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(p.stdout.strip(),'retired');self.assertFalse(self.marker.exists());self.assertEqual(self.log.read_text().strip(),str(self.profile))
    def test_missing_marker_requires_recorded_intent_and_retries_sync(self):
        self.marker.unlink();p=self.run_marker(allow=False);self.assertNotEqual(p.returncode,0)
        p=self.run_marker(allow=True);self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(p.stdout.strip(),'absent');self.assertTrue(self.log.exists())
    def test_missing_before_intent_is_reported_without_removal(self):
        self.marker.unlink();p=self.run_marker('inspect');self.assertEqual(p.returncode,0);self.assertEqual(p.stdout.strip(),'absent');self.assertFalse(self.log.exists())
    def test_foreign_expected_hash_never_unlinks(self):
        self.marker.write_bytes(b'1234567891\n');self.marker.chmod(0o600);p=self.run_marker(allow=True);self.assertNotEqual(p.returncode,0);self.assertTrue(self.marker.exists());self.assertFalse(self.log.exists())
    def test_noncanonical_contents_missing_lf_extra_lf_or_zero_refused(self):
        for raw in (b'1234567890',b'1234567890\n\n',b'01234567890\n',b'0\n',b'1234567890 \n'):
            with self.subTest(raw=raw):
                self.marker.write_bytes(raw);self.marker.chmod(0o600);p=self.run_marker(allow=True);self.assertNotEqual(p.returncode,0);self.assertTrue(self.marker.exists())
    def test_unsafe_parent_or_marker_mode_refused(self):
        self.profile.chmod(0o755);self.assertNotEqual(self.run_marker(allow=True).returncode,0);self.profile.chmod(0o700)
        self.marker.chmod(0o644);self.assertNotEqual(self.run_marker(allow=True).returncode,0);self.assertTrue(self.marker.exists())
    def test_foreign_owner_refused(self):
        p=self.run_marker(allow=True,FAKE_FOREIGN_OWNER=str(self.marker));self.assertNotEqual(p.returncode,0);self.assertTrue(self.marker.exists())
    def test_marker_symlink_hardlink_and_parent_symlink_refused(self):
        other=self.root/'other';self.marker.rename(other);self.marker.symlink_to(other);self.assertNotEqual(self.run_marker(allow=True).returncode,0);self.assertTrue(other.exists());self.marker.unlink();other.rename(self.marker)
        alias=self.root/'alias';os.link(self.marker,alias);self.assertNotEqual(self.run_marker(allow=True).returncode,0);alias.unlink()
        parent=self.root/'real-profile';self.profile.rename(parent);self.profile.symlink_to(parent,target_is_directory=True);self.assertNotEqual(self.run_marker(allow=True).returncode,0);self.assertTrue((parent/'gsf_android_id').exists())
    def test_completed_intent_refuses_any_recreated_marker(self):
        p=self.run_marker('verify_absent',True);self.assertNotEqual(p.returncode,0);self.assertTrue(self.marker.exists())
        self.marker.unlink();p=self.run_marker('verify_absent',True);self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
