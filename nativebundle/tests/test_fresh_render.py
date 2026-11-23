import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import subprocess
import sys
import shutil
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('fresh_render',ROOT/'source/runtime/redroid/fresh-bluejay-render.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def fixture(flags=4):
    data=bytearray(512);data[:6]=b'\x7fELF\x02\x01'
    struct.pack_into('<Q',data,32,64);struct.pack_into('<HH',data,54,56,1)
    struct.pack_into('<IIQQQQQQ',data,64,1,flags,0,0,0,512,512,4096)
    data[256:272]=m.SWIFT_OLD
    return bytes(data)


class SwiftTests(unittest.TestCase):
    def transform(self,data):
        with patch.multiple(m,SWIFT_SHA=hashlib.sha256(data).hexdigest(),SWIFT_SIZE=512,SWIFT_OFFSET=256):
            return m.swift_config_path(data)

    def test_only_readonly_literal_changes(self):
        before=fixture();after=self.transform(before)
        self.assertEqual(after[:256],before[:256]);self.assertEqual(after[272:],before[272:])
        self.assertEqual(after[256:272],b'/vendor/sw.ini\0\0')
        self.assertEqual(len(before),len(after))

    def test_executable_segment_rejected(self):
        with self.assertRaisesRegex(ValueError,'not_readonly'):self.transform(fixture(5))

    def test_wrong_base_hash_rejected(self):
        with self.assertRaisesRegex(ValueError,'base_changed'):m.swift_config_path(fixture())

    def test_duplicate_literal_rejected(self):
        value=bytearray(fixture());value[320:336]=m.SWIFT_OLD
        with self.assertRaisesRegex(ValueError,'literal_changed'):self.transform(bytes(value))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'payload').mkdir();self.p=self.root/'payload'
        (self.root/'Dockerfile').write_text('FROM scratch\nENTRYPOINT ["/init"]\n')
        (self.p/'xenoid.rc').write_text('on post-fs-data\n    exec_start xenoid-overlay\n')
        (self.p/'libgui.base.so').write_bytes(b'known-gui')
        (self.p/'swiftshader.base.so').write_bytes(fixture())
        for name in ('libgui_pacer.so','hwcomposer.raven.so','libpiex_shim.so'):
            (self.p/name).write_bytes(name.encode())
        self.profile={'build':{'device':'bluejay'},'display':{**m.DISPLAY,'defaultRefreshRateHz':60}}
        self.stack=patch.multiple(m,SWIFT_SHA=hashlib.sha256(fixture()).hexdigest(),SWIFT_SIZE=512,SWIFT_OFFSET=256)
        self.stack.start()

    def tearDown(self):self.stack.stop();self.temp.cleanup()

    def gui(self,data):return data+b':dependency',{'original_code_unchanged':True,'original_relocations_unchanged':True}

    def test_cold_manifest_regular_configs_properties_and_copy_bytes(self):
        result=m.prepare(self.root,self.profile,self.gui)
        self.assertEqual(result['properties'],m.PROPERTIES)
        self.assertEqual(result['regular_config_files'],['/vendor/etc/SwiftShader.ini','/vendor/sw.ini'])
        self.assertNotIn('symlinks',result)
        for target in result['regular_config_files']:
            self.assertEqual(result['artifactSha256'][target],hashlib.sha256(m.CONFIG).hexdigest())
        text=(self.root/'Dockerfile').read_text()
        self.assertIn('payload/libgui_pacer.so /system/lib64/libgui_pacer.so',text)
        self.assertNotIn('RUN ',text)
        self.assertNotIn('ln -s',text)
        expected={
            'payload/profile-swiftshader.ini':'/vendor/etc/SwiftShader.ini',
            'payload/profile-swiftshader-short.ini':'/vendor/sw.ini',
        }
        for source,target in expected.items():
            self.assertIn('COPY --chown=0:0 --chmod=0644 '+source+' '+target,text)
            path=self.root/source
            self.assertFalse(path.is_symlink())
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(),m.CONFIG)
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
        self.assertNotIn('--link',text);self.assertNotIn('/data/local/debug',text)
        for key,value in m.PROPERTIES.items():
            self.assertIn('    setprop '+key+' '+value+'\n',(self.p/'xenoid.rc').read_text())
        self.assertFalse((self.p/'libgui.base.so').exists());self.assertFalse((self.p/'swiftshader.base.so').exists())

    def test_wrong_geometry_rejected_before_output(self):
        self.profile['display']['width']=720
        with self.assertRaisesRegex(ValueError,'profile_display'):m.prepare(self.root,self.profile,self.gui)
        self.assertFalse((self.p/'fresh-bluejay-render.json').exists())

    def test_symlink_source_rejected(self):
        (self.p/'libgui.base.so').unlink();(self.p/'libgui.base.so').symlink_to('libgui_pacer.so')
        with self.assertRaisesRegex(ValueError,'regular_file_required'):m.prepare(self.root,self.profile,self.gui)

    def test_context_never_overwrites_existing_output(self):
        (self.p/'profile-libgui.so').write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError,'output_exists'):m.prepare(self.root,self.profile,self.gui)
        self.assertEqual((self.p/'profile-libgui.so').read_bytes(),b'foreign')

    def test_command_import_does_not_dirty_image_source_with_bytecode(self):
        scripts=self.root/'scripts';scripts.mkdir()
        for name in ('fresh-bluejay-render.py','profile-gui-dependency.py'):
            shutil.copyfile(ROOT/'source/runtime/redroid'/name,scripts/name)
        profile=self.root/'profile.json';profile.write_text(json.dumps(self.profile))
        # Intentionally fail the exact GUI base check after import. Python is
        # launched without -B: the helper itself must keep source immutable.
        result=subprocess.run([sys.executable,str(scripts/'fresh-bluejay-render.py'),
            '--context',str(self.root),'--profile',str(profile),'--profile-sha256',
            hashlib.sha256(profile.read_bytes()).hexdigest()],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((scripts/'__pycache__').exists())


if __name__=='__main__':unittest.main()
