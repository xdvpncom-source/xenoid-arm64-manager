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
        self.assertIn('qa_control_routes.py', REQUIRED)
        self.assertEqual(len(REQUIRED | {'default-bluejay-policy.json'}), 15)
        self.assertTrue(all((ROOT / 'qa' / name).is_file() for name in REQUIRED))

    def probe(self, include_vm, include_control=True):
        with tempfile.TemporaryDirectory(prefix='xenoid-worker-startup-') as temporary:
            folder = Path(temporary)
            for name in REQUIRED:
                if (include_vm or name != 'qa_host_memory.py') and (include_control or name != 'qa_control_routes.py'):
                    (folder / name).write_bytes((ROOT / 'qa' / name).read_bytes())
            (folder / 'default-bluejay-policy.json').write_bytes((ROOT / 'qa/default-bluejay-policy.json').read_bytes())
            (folder / 'disabled-fixture-config.json').write_text('{"enabled":false}\n')
            code = '''import ast,hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
def no_host_operations(event,args):
 if event in ('subprocess.Popen','os.system','socket.connect'):
  raise AssertionError('No native, network or input process in startup fixture')
 if event=='open'and isinstance(args[0],str)and args[0].startswith(('/proc/','/sys/','/data/','/opt/xenoid','/var/lib/xenoid')):
  raise AssertionError('No host or guest data in startup fixture')
sys.addaudithook(no_host_operations)
import qa_resource_runtime
# Execute the import statement from the actual timer/resource entrypoint in a
# fresh isolated tree. Missing controller-staged dependencies cannot fall back
# to the developer's checkout or site packages.
source=ast.parse((root/'qa_resource_runtime.py').read_text())
entry=next(n for n in source.body if isinstance(n,ast.FunctionDef)and n.name=='reconcile_owned')
route_import=next(n for n in ast.walk(entry)if isinstance(n,ast.ImportFrom)and n.module=='qa_control_routes')
exec(compile(ast.Module(body=[route_import],type_ignores=[]),'<actual-resource-route-import>','exec'))
import qa_control_routes
assert Path(qa_control_routes.__file__).resolve().parent==root
assert ensure_control_routes is qa_control_routes.ensure_control_routes
if sys.argv[2]=='yes':
 import qa_host_memory
 assert Path(qa_host_memory.__file__).resolve().parent==root
 # This explicit permission double selects only the actual disabled-config
 # branch; no import stub or replacement guest-guard function is used.
 qa_host_memory.os.geteuid=lambda:0
 qa_host_memory.CONFIG=root/'disabled-fixture-config.json'
answer=qa_resource_runtime.ensure_host_vm_guard(None,{'Id':'a'*64})
print(json.dumps({'answer':answer,'module_sha256':hashlib.sha256((root/'qa_host_memory.py').read_bytes()).hexdigest(),'route_module_sha256':hashlib.sha256((root/'qa_control_routes.py').read_bytes()).hexdigest()}))
'''
            return subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
                str(folder), 'yes' if include_vm else 'no'], capture_output=True,
                text=True, timeout=15)

    def test_worker_without_vm_dependency_reproduces_missing_startup_module(self):
        result = self.probe(False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'qa_host_memory'", result.stderr)

    def test_worker_without_route_dependency_fails_actual_entrypoint_import(self):
        result = self.probe(True, include_control=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'qa_control_routes'", result.stderr)

    def test_15_file_worker_loads_real_route_module_and_runs_resource_vm_guard(self):
        result = self.probe(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['answer'], {'ok': True, 'skipped': 'host_vm_isolation_disabled'})
        self.assertEqual(observed['module_sha256'],
            hashlib.sha256((ROOT / 'qa/qa_host_memory.py').read_bytes()).hexdigest())
        self.assertEqual(observed['route_module_sha256'],
            hashlib.sha256((ROOT / 'qa/qa_control_routes.py').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
