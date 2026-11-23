import importlib.util,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
P=Path(__file__).resolve().parents[1]/'scripts/repair-merged-lib64.py';s=importlib.util.spec_from_file_location('repair',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Layout(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.owner=os.getuid();(self.root/'usr').mkdir(mode=0o755)
  for name in('bin','sbin','lib'):(self.root/name).symlink_to('usr/'+name)
  p=self.root/m.TARGET.lstrip('/');p.parent.mkdir(parents=True);r=bytearray(64);r[:6]=b'\x7fELF\x02\x01';r[18:20]=(62).to_bytes(2,'little');p.write_bytes(r);p.chmod(0o644)
  (self.root/'lib64').mkdir(mode=0o755);(self.root/'lib64'/m.LOADER).symlink_to(m.TARGET)
 def plan(self):return m.inspect_layout(self.root,self.owner,False)
 def test_known_single_loader_only(self):
  self.assertEqual(self.plan()['action'],'repair_owned_legacy_lib64');self.assertFalse((self.root/'usr/lib64').exists())
 def test_unknown_file_or_target_refused(self):
  p=self.root/'lib64/other';p.write_text('keep')
  with self.assertRaisesRegex(RuntimeError,'unrecognized_files'):self.plan()
  p.unlink();p=self.root/'lib64'/m.LOADER;p.unlink();p.symlink_to('/wrong')
  with self.assertRaisesRegex(RuntimeError,'unrecognized_loader_link'):self.plan()
 def test_nonempty_destination_refused(self):
  p=self.root/'usr/lib64';p.mkdir();(p/'keep').write_text('keep')
  with self.assertRaisesRegex(RuntimeError,'not_empty'):self.plan()
 def test_merged_layout_untouched(self):
  (self.root/'lib64'/m.LOADER).unlink();(self.root/'lib64').rmdir();(self.root/'usr/lib64').mkdir();(self.root/'lib64').symlink_to('usr/lib64');old=m.identity(self.root/'lib64')
  self.assertFalse(m.apply_layout(self.root,self.owner,False)['changed']);self.assertEqual(old,m.identity(self.root/'lib64'))
 def exchange(self,a,b):
  tmp=a.parent/'exchange-temp';os.rename(a,tmp);os.rename(b,a);os.rename(tmp,b)
 def test_migration_preserves_directory_and_loader(self):
  old=m.identity(self.root/'lib64');target=self.root/m.TARGET.lstrip('/');data=target.read_bytes()
  with patch.object(m,'exchange',side_effect=self.exchange):r=m.apply_layout(self.root,self.owner,False)
  self.assertTrue(r['changed']);self.assertEqual(os.readlink(self.root/'lib64'),'usr/lib64');self.assertEqual(target.read_bytes(),data);self.assertEqual(m.identity(Path(r['backup'])/'lib64-before'),old)
 def test_failure_restores_original_layout(self):
  old=m.identity(self.root/'lib64')
  with patch.object(m,'exchange',side_effect=self.exchange),patch.object(m,'syncdir',side_effect=[RuntimeError('readback'),None]):
   with self.assertRaisesRegex(RuntimeError,'readback'):m.apply_layout(self.root,self.owner,False)
  self.assertEqual(m.identity(self.root/'lib64'),old);self.assertFalse((self.root/'lib64').is_symlink());self.assertFalse((self.root/'usr/lib64').exists())
 def test_installer_repairs_before_apt_and_toolchain_never_creates_real_lib64(self):
  root=P.parents[1];installer=(root/'install.sh').read_text();tool=(root/'install-toolchain.sh').read_text()
  self.assertLess(installer.index('python3 "$loader_helper" --execute'),installer.index('apt-get update'))
  block=tool[tool.index('ensure_amd64_compatibility()'):tool.index('sdk_ready()')]
  self.assertLess(block.index('python3 "$layout_helper" --execute'),block.index('apt-get '))
  self.assertNotIn('install -d -m 0755 /lib64',tool)
 def package_call(self,pending,args,clear=True):
  source=(P.parents[1]/'install.sh').read_text();body=source[source.index('install_packages() {'):source.index("\nprintf 'Preparing Ubuntu build dependencies")]
  calls=self.root/'apt-calls';done=self.root/'configured'
  code='''set -euo pipefail
apt_ready=0
package_installed() { [[ $1 == installed ]]; }
dpkg() { if [[ $pending == 1 && ! -f $done ]]; then printf pending; fi; }
apt-get() { printf '%s\\n' "$*" >> "$calls"; if [[ $1 == install && $clear == 1 ]]; then touch "$done"; fi; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
'''+body+'\ninstall_packages '+args+'\n'
  env=dict(os.environ,pending='1'if pending else'0',clear='1'if clear else'0',calls=str(calls),done=str(done))
  result=subprocess.run(['bash','-c',code],env=env,capture_output=True,text=True)
  return result,calls.read_text().splitlines()if calls.exists()else[]
 def test_clean_installed_packages_do_not_run_apt(self):
  result,calls=self.package_call(False,'installed');self.assertEqual(result.returncode,0);self.assertEqual(calls,[])
 def test_pending_packages_are_repaired_without_removal_or_broad_upgrade(self):
  result,calls=self.package_call(True,'installed');self.assertEqual(result.returncode,0);self.assertEqual(calls,['update','install --fix-broken --no-remove -y --no-install-recommends'])
 def test_new_dependency_keeps_explicit_package_and_no_remove(self):
  result,calls=self.package_call(False,'installed missing');self.assertEqual(result.returncode,0);self.assertEqual(calls[-1],'install --fix-broken --no-remove -y --no-install-recommends missing')
 def test_unrepaired_state_fails_before_native_startup(self):
  result,_=self.package_call(True,'installed',clear=False);self.assertNotEqual(result.returncode,0);self.assertIn('remains incomplete',result.stderr)
if __name__=='__main__':unittest.main()
