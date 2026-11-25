"""Exercise the actual lazy startup import in a fresh self-contained worker tree."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'scripts/update-release-pins.py').read_text())
REQUIRED = next(ast.literal_eval(node.value) for node in TREE.body
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                    and target.id == 'required_host_modules' for target in node.targets))


class HostHelperClosure(unittest.TestCase):
    def test_declared_worker_closure_includes_the_lazy_vm_guard_dependency(self):
        self.assertIn('qa_host_memory.py', REQUIRED)
        self.assertEqual(len(REQUIRED | {'default-bluejay-policy.json'}), 14)
        self.assertTrue(all((ROOT / 'qa' / name).is_file() for name in REQUIRED))

    def probe(self, include_vm):
        with tempfile.TemporaryDirectory(prefix='xenoid-worker-startup-') as temporary:
            folder = Path(temporary)
            for name in REQUIRED:
                if include_vm or name != 'qa_host_memory.py':
                    (folder / name).write_bytes((ROOT / 'qa' / name).read_bytes())
            (folder / 'disabled-fixture-config.json').write_text('{"enabled":false}\n')
            code = '''import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
import qa_resource_runtime
if sys.argv[2]=='yes':
 import qa_host_memory
 assert Path(qa_host_memory.__file__).resolve().parent==root
 # This explicit permission double selects only the actual disabled-config
 # branch; no import stub or replacement guest-guard function is used.
 qa_host_memory.os.geteuid=lambda:0
 qa_host_memory.CONFIG=root/'disabled-fixture-config.json'
 def no_host_operations(event,args):
  if event in ('subprocess.Popen','os.system','socket.connect'):
   raise AssertionError('No native, network or input process in startup fixture')
  if event=='open'and isinstance(args[0],str)and args[0].startswith(('/proc/','/sys/','/data/','/opt/','/var/lib/xenoid')):
   raise AssertionError('No host or guest data in startup fixture')
 sys.addaudithook(no_host_operations)
answer=qa_resource_runtime.ensure_host_vm_guard(None,{'Id':'a'*64})
print(json.dumps({'answer':answer,'module_sha256':hashlib.sha256((root/'qa_host_memory.py').read_bytes()).hexdigest()}))
'''
            return subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
                str(folder), 'yes' if include_vm else 'no'], capture_output=True,
                text=True, timeout=15)

    def test_13_file_worker_reproduces_the_observed_missing_startup_module(self):
        result = self.probe(False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'qa_host_memory'", result.stderr)

    def test_14_file_worker_runs_the_real_resource_and_vm_guard_bodies(self):
        result = self.probe(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['answer'], {'ok': True, 'skipped': 'host_vm_isolation_disabled'})
        self.assertEqual(observed['module_sha256'],
            hashlib.sha256((ROOT / 'qa/qa_host_memory.py').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
