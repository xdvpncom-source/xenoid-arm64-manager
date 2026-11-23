"""Bind the shipped upstream patch and upgrade allowlist to the exercised native fixtures."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import test_proxy_counter_acceptance as observer
import test_proxy_engine_commit as engine

ROOT = Path(__file__).resolve().parents[1]


class ProxyPatchReleaseTests(unittest.TestCase):
    def test_shipped_patch_produces_the_exact_tested_observer_and_commit(self):
        # Undo the single older Fleet quarantine patch to reconstruct the pinned
        # stock module, then exercise the retained historical source patch.
        previous = '''        # Planner quarantine intent is distinct from the host's live state.
        host_quarantined = (
            _at(snapshot, "proxyQuarantined") is True
            or host.get("quarantined") is True
            or host.get("phase") in ("quarantine", "quarantined")
        )
        checks.add("proxyHostQuarantine", not host_quarantined, "proxy_quarantined")
'''
        original = '''        snapshot_quarantine = _first_bool(
            _at(snapshot, "proxyQuarantined"),
            _at(snapshot, "components.proxy.quarantineRequired"),
            _at(host, "quarantined"),
        )
        if snapshot_quarantine is True:
            checks.add("proxyHostQuarantine", False, "proxy_quarantined")
        else:
            checks.add("proxyHostQuarantine", True, "proxy_quarantined")
'''
        self.assertEqual(observer.OLD_SOURCE.count(previous), 1)
        stock = observer.OLD_SOURCE.replace(previous, original)
        payload = stock.encode()
        self.assertEqual(hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest(),
                         '4927d80992d2f85fdc7b152f579c5c3865c94442')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'src/xenoid').mkdir(parents=True)
            (root/'scripts').mkdir()
            (root/'src/xenoid/live_observe.py').write_text(stock)
            (root/'scripts/xenoid-proxy-engine.py').write_text('\n'*1037+engine.OLD_APPLY)
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'apply','--include=src/xenoid/live_observe.py',
                '--include=scripts/xenoid-proxy-engine.py',str(ROOT/'patches/proxy-live-acceptance.patch')],
                check=True,capture_output=True)
            self.assertEqual((root/'src/xenoid/live_observe.py').read_text(), observer.NEW_SOURCE)
            actual = ast.parse((root/'scripts/xenoid-proxy-engine.py').read_text())
            expected = ast.parse(engine.NEW_APPLY)
            self.assertEqual(ast.dump(actual), ast.dump(expected))

    def test_native_bundle_accepts_the_exact_previous_fleet_observer(self):
        source = observer.OLD_SOURCE.encode()
        manifest = json.loads((ROOT/'nativebundle/manifest.json').read_text())
        row = next(item for item in manifest['files'] if item['path'] == 'src/xenoid/live_observe.py')
        self.assertIn(hashlib.sha256(source).hexdigest(), row['before_sha256'])
        self.assertNotIn(hashlib.sha256(source + b'changed').hexdigest(), row['before_sha256'])
        self.assertEqual(hashlib.sha256((ROOT/'nativebundle/source'/row['path']).read_bytes()).hexdigest(), row['sha256'])


if __name__ == '__main__': unittest.main()
