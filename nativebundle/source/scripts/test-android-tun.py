#!/usr/bin/env python3
"""Focused canonical TUN contracts, including real Linux device/xattr tests."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("android_tun", ROOT / "scripts/ensure-android-tun.py")
tun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tun)
sys.path.insert(0, str(ROOT / "src"))
from xenoid.backend import RuntimeManager


class Contracts(unittest.TestCase):
    def test_exact_metadata(self):
        def meta(**kwargs):
            return SimpleNamespace(**dict(st_mode=stat.S_IFCHR | 0o660, st_rdev=tun.DEVICE,
                                         st_uid=1000, st_gid=1016, **kwargs))
        tun.validate_node(meta(), tun.LABEL)
        for field, value in (("st_mode", stat.S_IFREG | 0o660), ("st_rdev", os.makedev(1, 3)),
                             ("st_uid", 0), ("st_gid", 0), ("st_mode", stat.S_IFCHR | 0o666)):
            node = meta();setattr(node, field, value)
            with self.assertRaises(RuntimeError):tun.validate_node(node, tun.LABEL)
        for label in (b"", b"u:object_r:device:s0\0", tun.LABEL + b"bad"):
            with self.assertRaises(RuntimeError):tun.validate_node(meta(), label)

    def test_backend_preserves_helper_failure_and_binds_identity(self):
        manager = object.__new__(RuntimeManager)
        manager.context = SimpleNamespace(project_root=ROOT, instance_id="11111111-2222-4333-8444-555555555555")
        manager.lease = SimpleNamespace(resource_tag="abcdef123456")
        manager._owned_container_record = lambda **kw: ({"Id": "a"*64, "Image": "sha256:"+"b"*64}, None)
        commands=[]
        def execute(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=1, stdout=json.dumps({"schema":tun.SCHEMA,
                "ok":False,"ready":False,"error":"android_tun_node_conflict"}))
        manager._engine_host_shell=execute
        self.assertEqual(manager._android_tun_state(ensure=True)["error"], "android_tun_node_conflict")
        self.assertIn("11111111-2222-4333-8444-555555555555",commands[0])
        self.assertIn("abcdef123456",commands[0])
        self.assertIn("ensure",commands[0])

    def test_source_uses_up_observation_and_authenticated_bootstrap(self):
        source=(ROOT/"src/xenoid/backend.py").read_text()
        self.assertIn('android_tun = self._android_tun_state() if state == "running"',source)
        self.assertIn('and android_tun.get("ready") is True',source)
        start=source.index("    def reconcile_bootstrap(")
        end=source.index("    def _rootd_exec(",start)
        body=source[start:end]
        self.assertLess(body.index("if rootd.get(\"ok\") is not True:"),body.index("self._android_tun_state(ensure=True"))
        self.assertLess(body.index("self._android_tun_state(ensure=True"),body.index("client.bootstrap_reconcile("))


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid()==0,
                     "real character-device tests require Linux root")
class LinuxNodes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        self.fd=os.open(self.path,os.O_RDONLY|os.O_DIRECTORY)
        probe=self.path/"probe";probe.write_bytes(b"")
        try:os.setxattr(probe,"security.selinux",tun.LABEL)
        except OSError as error:
            os.close(self.fd);self.temp.cleanup();self.skipTest(str(error))
        probe.unlink()
    def tearDown(self):
        os.close(self.fd);self.temp.cleanup()
    def test_create_then_noop_preserves_linux_node(self):
        os.mkdir(self.path/"net")
        os.mknod(self.path/"net/tun",stat.S_IFCHR|0o666,tun.DEVICE)
        before=os.lstat(self.path/"net/tun")
        self.assertTrue(tun.ensure_node(self.fd,repair=True,checkpoint=lambda:None)["created"])
        inode=os.lstat(self.path/"tun").st_ino
        self.assertFalse(tun.ensure_node(self.fd,repair=True,checkpoint=lambda:None)["created"])
        self.assertEqual(inode,os.lstat(self.path/"tun").st_ino)
        self.assertEqual(os.lstat(self.path/"net/tun"),before)
        self.assertEqual(list(self.path.glob(".xenoid-tun-*")),[])
    def test_inspect_missing_has_no_mutation(self):
        self.assertEqual(tun.ensure_node(self.fd,repair=False,checkpoint=lambda:None)["error"],"android_tun_missing")
        self.assertEqual(list(self.path.iterdir()),[])
    def test_regular_or_symlink_conflict_not_overwritten(self):
        for symlink in (False,True):
            path=self.path/"tun"
            if symlink:path.symlink_to("net/tun")
            else:path.write_bytes(b"untouched")
            before=path.lstat()
            with self.assertRaises(RuntimeError):tun.ensure_node(self.fd,repair=True,checkpoint=lambda:None)
            self.assertEqual(path.lstat(),before)
            path.unlink()
    def test_wrong_device_or_metadata_not_rewritten(self):
        for number,mode in ((os.makedev(1,3),0o660),(tun.DEVICE,0o666)):
            path=self.path/"tun";os.mknod(path,stat.S_IFCHR|mode,number)
            before=path.lstat()
            with self.assertRaises(RuntimeError):tun.ensure_node(self.fd,repair=True,checkpoint=lambda:None)
            self.assertEqual(path.lstat(),before);path.unlink()
    def test_epoch_change_rolls_back_only_own_published_node(self):
        count=0
        def checkpoint():
            nonlocal count
            count+=1
            if count==3:raise RuntimeError("android_tun_runtime_changed")
        with self.assertRaisesRegex(RuntimeError,"runtime_changed"):
            tun.ensure_node(self.fd,repair=True,checkpoint=checkpoint)
        self.assertEqual(list(self.path.iterdir()),[])
    def test_competing_node_is_not_overwritten_or_deleted(self):
        count=0
        def checkpoint():
            nonlocal count
            count+=1
            if count==2:(self.path/"tun").write_bytes(b"competitor")
        with self.assertRaises(FileExistsError):tun.ensure_node(self.fd,repair=True,checkpoint=checkpoint)
        self.assertEqual((self.path/"tun").read_bytes(),b"competitor")
        self.assertEqual(list(self.path.glob(".xenoid-tun-*")),[])


if __name__ == "__main__":unittest.main()
