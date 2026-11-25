"""Compile and exercise the actual proc-label reader without mounts or Android."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'nativebundle/source'


class HardwareLabel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('cc'):
            raise unittest.SkipTest('C compiler required')
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        source = (ROOT / 'native/xenoid-hide/xenoid_overlay.c').read_text()
        start = source.index('static int load_profile_hardware(')
        end = source.index('\nstatic int overlay_version(', start)
        cls.actual_functions = source[start:end]
        stub = r'''
#define _GNU_SOURCE
#define XENOID_MODEL_PROFILE_TESTING
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#define PROFILE_DIR "."
struct cpu_profile_view { char implementer[16], part[8][16], model[8][65]; };
static void load_cpu_profile(struct cpu_profile_view *v) {
 memset(v,0,sizeof(*v));strcpy(v->implementer,"0x41");
 for(int n=0;n<8;n++){strcpy(v->part[n],"0xd05");strcpy(v->model[n],"test-cpu");}
}
static int overlay_text(const char *name,const char *path,const char *text) {
 if(strcmp(name,"cpuinfo")||strcmp(path,"/proc/cpuinfo"))return 3;
 fputs(text,stdout);return 0;
}
'''
        fixture = cls.directory / 'probe.c'
        fixture.write_text(stub + cls.actual_functions + '\nint main(void){return overlay_cpuinfo()?2:0;}\n')
        cls.binary = cls.directory / 'probe'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        str(fixture), '-o', str(cls.binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.folder = Path(self.tempdir.name)
        self.leaf = self.folder / 'build_hardware'

    def tearDown(self):
        self.tempdir.cleanup()

    def probe(self):
        return subprocess.run([str(self.binary)], cwd=self.folder,
                              capture_output=True, text=True, timeout=5)

    def test_selected_bluejay_changes_only_existing_hardware_line(self):
        old = self.probe()
        self.assertEqual(old.returncode, 0)
        self.leaf.write_text('bluejay\n')
        self.leaf.chmod(0o600)
        new = self.probe()
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout, old.stdout.replace('Hardware\t: raven\n',
                                                       'Hardware\t: bluejay\n'))

    def test_explicit_raven_and_absent_leaf_preserve_legacy(self):
        old = self.probe()
        self.leaf.write_text('raven\n'); self.leaf.chmod(0o600)
        self.assertEqual(self.probe().stdout, old.stdout)

    def test_unknown_malformed_or_multiline_never_published(self):
        for value in ('other\n', 'bluejay', 'bluejay\nextra', 'bluejay\x00raven\n', '\n', 'x'*40):
            with self.subTest(value=repr(value)):
                self.leaf.write_text(value); self.leaf.chmod(0o600)
                actual = self.probe()
                self.assertNotEqual(actual.returncode, 0)
                self.assertEqual(actual.stdout, '')

    def test_symlink_hardlink_and_writable_leaf_rejected(self):
        target = self.folder / 'target'; target.write_text('bluejay\n'); target.chmod(0o600)
        self.leaf.symlink_to(target)
        self.assertNotEqual(self.probe().returncode, 0)
        self.leaf.unlink(); os.link(target, self.leaf)
        self.assertNotEqual(self.probe().returncode, 0)
        self.leaf.unlink(); self.leaf.write_text('bluejay\n'); self.leaf.chmod(0o666)
        self.assertNotEqual(self.probe().returncode, 0)

    def test_fifo_is_rejected_without_waiting_for_writer(self):
        os.mkfifo(self.leaf, 0o600)
        actual = self.probe()
        self.assertNotEqual(actual.returncode, 0)
        self.assertEqual(actual.stdout, "")

    def test_producer_leaf_and_no_host_kernel_security_change(self):
        seed = (ROOT / 'native/xenoid-profile-runtime/cold_seed.c').read_text()
        java = (ROOT / 'daemon/app/src/main/java/dev/xenoid/daemon/DeviceProfileManager.java').read_text()
        self.assertIn('add(p,count,j,"build_hardware","build.hardware")', seed)
        self.assertIn('fields.put("build_hardware", hardware)', java)
        self.assertNotIn('setenforce', self.actual_functions)
        self.assertNotIn('mount(', self.actual_functions)
        self.assertNotIn('overlay_version', self.actual_functions)


@unittest.skipUnless(shutil.which('javac') and shutil.which('java'), 'JDK required')
class JavaHardwareProducer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        source = (ROOT / 'daemon/app/src/main/java/dev/xenoid/daemon/DeviceProfileManager.java').read_text()
        start = source.index('    private static Map<String,String> prepareHardwareProfile(')
        end = source.index('        JSONObject cpu =', start)
        prefix = source[start:end].replace('Map<String,String> prepareHardwareProfile',
                                            'String prepareHardwareProfile')
        start = source.index('    private static String requiredString(')
        end = source.index('\n    private static ', start + 10)
        required = source[start:end]
        harness = '''import java.util.*;
class JSONObject {
 static final Object NULL=new Object(); final Map<String,Object> values=new HashMap<>();
 Object opt(String key){return values.get(key);}
 JSONObject optJSONObject(String key){Object value=opt(key);return value instanceof JSONObject?(JSONObject)value:null;}
}
public class HardwareProducer {
'''+prefix+'''        return hardware;
    }
'''+required+'''
 public static void main(String[] args)throws Exception {
  JSONObject profile=new JSONObject();
  if(args.length>0){JSONObject build=new JSONObject();build.values.put("hardware",args[0]);profile.values.put("build",build);}
  try{System.out.println(prepareHardwareProfile(profile));}
  catch(IllegalArgumentException error){System.exit(2);}
 }
}
'''
        java = cls.folder / 'HardwareProducer.java'; java.write_text(harness)
        subprocess.run(['javac', '-d', str(cls.folder), str(java)],
                       capture_output=True, check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_validation_accepts_only_supported_hardware(self):
        for value in ('bluejay', 'raven'):
            result = subprocess.run(['java', '-cp', str(self.folder), 'HardwareProducer', value],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, value))
        for args in ([], ['unknown'], ['bluejay\n'], ['']):
            result = subprocess.run(['java', '-cp', str(self.folder), 'HardwareProducer', *args],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout), (2, ''))


if __name__ == '__main__':
    unittest.main()
