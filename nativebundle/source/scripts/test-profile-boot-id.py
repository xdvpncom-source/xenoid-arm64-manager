#!/usr/bin/env python3
"""Atomic boot-file tests; Linux additionally exercises real xattrs."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('boot_seed',ROOT/'scripts/seed-profile-boot-id.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
OLD='11111111-2222-4333-8444-555555555555'
NEW='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

class AtomicBootFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.path=self.directory/'boot_id';self.path.write_text(OLD+'\n');self.path.chmod(0o640)
        self.attrs={'security.selinux':seed.LABEL,'user.example':b'preserve me'}
        self.read=patch.object(seed,'read_attrs',side_effect=lambda fd:dict(self.attrs));self.read.start()
        self.write=patch.object(seed,'write_attrs');self.write.start()
    def tearDown(self):self.write.stop();self.read.stop();self.temp.cleanup()
    def run_seed(self):return seed.replace_boot_id(self.directory,NEW,owner_uid=os.geteuid())
    def test_noop_preserves_inode_and_metadata(self):
        before=self.path.stat()
        result=seed.replace_boot_id(self.directory,OLD,owner_uid=os.geteuid())
        self.assertTrue(result['already']);self.assertEqual(self.path.stat().st_ino,before.st_ino)
    def test_changed_value_is_atomic_and_preserves_owner_mode(self):
        before=self.path.stat();result=self.run_seed();after=self.path.stat()
        self.assertFalse(result['already']);self.assertEqual(self.path.read_text(),NEW+'\n')
        self.assertEqual((after.st_uid,after.st_gid,after.st_mode),(before.st_uid,before.st_gid,before.st_mode))
        self.assertEqual(list(self.directory.iterdir()),[self.path])
    def test_staging_failure_preserves_original(self):
        with patch.object(seed,'write_attrs',side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError):self.run_seed()
        self.assertEqual(self.path.read_text(),OLD+'\n');self.assertEqual(list(self.directory.iterdir()),[self.path])
    def test_conflicting_or_signature_metadata_rejected(self):
        for attrs in ({'security.selinux':b'wrong'}, {'security.selinux':seed.LABEL,'security.ima':b'signature'}):
            self.attrs=attrs
            with self.assertRaises(seed.BootSeedError):self.run_seed()
            self.assertEqual(self.path.read_text(),OLD+'\n')
    def test_symlink_target_is_rejected(self):
        self.path.rename(self.directory/'original');self.path.symlink_to('original')
        with self.assertRaises(OSError):self.run_seed()
        self.assertEqual((self.directory/'original').read_text(),OLD+'\n')
    @unittest.skipUnless(hasattr(os,'setxattr'),'real Linux xattr API required')
    def test_real_xattrs_survive_atomic_replace(self):
        self.write.stop();self.read.stop()
        os.setxattr(self.path,'user.example',b'actual xattr')
        # A real security.selinux label requires privileged Linux scratch.
        try:os.setxattr(self.path,'security.selinux',seed.LABEL)
        except PermissionError:self.skipTest('privileged security xattrs required')
        self.run_seed();self.assertEqual(os.getxattr(self.path,'security.selinux'),seed.LABEL)
        self.assertEqual(os.getxattr(self.path,'user.example'),b'actual xattr')
        self.read.start();self.write.start()

if __name__=='__main__':unittest.main()
