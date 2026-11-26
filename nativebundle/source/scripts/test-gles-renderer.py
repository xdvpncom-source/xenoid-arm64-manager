#!/usr/bin/env python3
"""Runtime-free producer transform and native mount failure-boundary checks.

Set XENOID_GLES_ORIGINAL to a privately extracted pinned base library to run the
real-ELF tests. No vendor library, APK, runtime state or capture is in this test.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gles_renderer", ROOT / "scripts/profile-gles-renderer.py")
gles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gles)
FIXTURE = os.environ.get("XENOID_GLES_ORIGINAL")


class LabelContract(unittest.TestCase):
    def test_labels(self):
        for label in (None, "Mali-G78", "QA renderer", "a" * 128):
            self.assertEqual(gles.normalize_label(label), label)
        for label in ("", " ", " x", "x ", "a\n", "a\0b", "é", "a" * 129, 4, False):
            with self.subTest(label=label), self.assertRaises(ValueError):
                gles.normalize_label(label)

    def test_unknown_original_fails_before_transform(self):
        for raw in (b"", b"\x7fELF", b"a" * gles.INPUT_SIZE):
            with self.assertRaisesRegex(ValueError, "unknown_original_build"):
                gles.transform(raw)


@unittest.skipUnless(FIXTURE, "private pinned base ELF not supplied")
class PinnedProducer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = Path(FIXTURE).read_bytes()
        cls.output, cls.metadata = gles.transform(cls.original)

    def test_canary_output_is_reproducible(self):
        self.assertEqual(len(self.output), 8032888)
        self.assertEqual(hashlib.sha256(self.output).hexdigest(),
                         "dbe3f37c4994eef44afb987a4efff8168e86f83f132a1659558b7d9e225a5070")
        self.assertEqual(gles.transform(self.original), (self.output, self.metadata))
        self.assertTrue(gles.verify(self.original, self.output, self.metadata)["ok"])

    def test_disabled_is_exact_original(self):
        output, metadata = gles.transform(self.original, None)
        self.assertEqual(output, self.original)
        self.assertFalse(metadata["enabled"])
        self.assertTrue(gles.verify(self.original, output, metadata)["ok"])

    def test_variable_labels_preserve_other_bytes(self):
        for label in ("X", "Mali-G78", "QA graphics " + "x" * 110):
            output, metadata = gles.transform(self.original, label)
            self.assertTrue(gles.verify(self.original, output, metadata)["ok"])
            self.assertIn(label.encode() + b"\0", output[len(self.original):])
            restored = bytearray(output[:len(self.original)])
            for a, b in ((32, 40), (56, 58), (gles.CASE_VA, gles.CASE_VA + 4)):
                restored[a:b] = self.original[a:b]
            self.assertEqual(bytes(restored), self.original)

    def test_forged_manifests_cannot_hide_corruption(self):
        for offset in (1, 40, 64, 0x298382, gles.CASE_VA, gles.CASE_VA + 4,
                       0x7a9000, 0x7a900f, 0x7a9010, len(self.output) - 1):
            damaged = bytearray(self.output)
            damaged[offset] ^= 1
            metadata = copy.deepcopy(self.metadata)
            metadata["output_sha256"] = hashlib.sha256(damaged).hexdigest()
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                gles.verify(self.original, bytes(damaged), metadata)

    def test_original_digest_required(self):
        damaged = bytearray(self.original)
        damaged[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "unknown_original_build"):
            gles.transform(bytes(damaged))


@unittest.skipUnless(shutil.which("cc"), "host C compiler unavailable")
class NativeBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="renderer-native-test-")
        cls.directory = Path(cls.temporary.name)
        original = (ROOT / "native/xenoid-renderer/xenoid_renderer.c").read_text()
        # Compile the real operation dispatcher with deterministic syscall and
        # readback faults. The production file has no test switch or bypass.
        mount_header = '''
#define MS_BIND 4096
#define MS_PRIVATE (1<<18)
#define MS_REMOUNT 32
#define MS_RDONLY 1
#define MNT_DETACH 2
#define UMOUNT_NOFOLLOW 8
int mock_mount(const char *, const char *, const char *, unsigned long, const void *);
int mock_umount2(const char *, int);
#define mount mock_mount
#define umount2 mock_umount2
'''
        original = original.replace('#include <sys/mount.h>', mount_header)
        original = original.replace('static int inspect(', 'static int real_inspect(', 1)
        position = original.index('int main(int argc,char **argv)')
        injected = '''
static int calls, unmount_calls, flags_seen, inspect_calls;
static int fail_mount, detach_failure, before_state, after_state, initial_writable;
int mock_mount(const char *a,const char *b,const char *c,unsigned long d,const void *e) {
 (void)a;(void)b;(void)c;(void)d;(void)e;calls++;errno=EPERM;return calls==fail_mount?-1:0;
}
int mock_umount2(const char *a,int flags) {
 (void)a;unmount_calls++;flags_seen=flags;errno=EBUSY;return detach_failure?-1:0;
}
static uid_t mock_uid(void) {return 0;}
static int mock_readonly(const char *a) {(void)a;return !initial_writable||calls>=2;}
static int mock_inspect(const char *a,const char *b,uint64_t c,uint64_t d,uint64_t e,uint64_t f,struct stat *s,struct stat *t) {
 (void)a;(void)b;(void)e;(void)f;memset(s,0,sizeof(*s));memset(t,0,sizeof(*t));
 s->st_dev=t->st_dev=(dev_t)c;s->st_ino=t->st_ino=(ino_t)d;
 return inspect_calls++?after_state:before_state;
}
static int mock_lstat(const char *a,struct stat *s) {(void)a;memset(s,0,sizeof(*s));s->st_dev=1;s->st_ino=2;return 0;}
#define geteuid mock_uid
#define readonly mock_readonly
#define inspect mock_inspect
#define lstat mock_lstat
#define main operation_main
'''
        original = original[:position] + injected + original[position:]
        original += '''
#undef main
int main(int argc,char **argv) {
 if(argc!=7)return 8;
 fail_mount=atoi(argv[2]);detach_failure=atoi(argv[3]);before_state=atoi(argv[4]);after_state=atoi(argv[5]);initial_writable=atoi(argv[6]);
 if(!strcmp(argv[1],"hash")) {
   sha256 state;char hex[65];sha_init(&state);
   for(int i=0;i<atoi(argv[6]);i++)sha_update(&state,(const unsigned char *)"a",1);
   sha_final(&state,hex);puts(hex);return 0;
 }
 char *args[]={"renderer",argv[1],"/unused","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","1","2","3","4",NULL};
 int result=operation_main(8,args);
 fprintf(stderr,"%d %d %d %d\\n",calls,unmount_calls,flags_seen,inspect_calls);return result;
}
'''
        source = cls.directory / "boundaries.c"
        source.write_text(original)
        cls.binary = cls.directory / "boundaries"
        subprocess.run([shutil.which("cc"), "-std=c11", "-O0", str(source), "-o", str(cls.binary)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_case(self, command, fail=0, detach=0, before=0, after=1, count=0):
        result = subprocess.run([str(self.binary), command, str(fail), str(detach), str(before),
                                 str(after), str(count)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=10)
        return result

    def test_sha256_known_vectors(self):
        for count in (0, 1, 55, 56, 64, 65, 1000000):
            result = self.run_case("hash", count=count)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), hashlib.sha256(b"a" * count).hexdigest())

    def test_native_status_and_idempotent_bind(self):
        native = self.run_case("status", before=0)
        self.assertEqual(json.loads(native.stdout)["state"], "native")
        bound = self.run_case("bind", before=1)
        self.assertFalse(json.loads(bound.stdout)["changed"])
        self.assertEqual(bound.stderr.strip(), "0 0 0 1")

    def test_each_mount_failure_restores_only_created_bind(self):
        first = self.run_case("bind", fail=1)
        self.assertEqual(json.loads(first.stdout)["error"], "bind_failed")
        self.assertEqual(first.stderr.strip(), "1 0 0 1")
        for fail in (2, 3):
            result = self.run_case("bind", fail=fail)
            self.assertEqual(json.loads(result.stdout)["error"], "bind_setup_failed_restored")
            self.assertEqual(result.stderr.strip(), f"{fail} 1 10 1")

    def test_failed_cleanup_is_explicit(self):
        result = self.run_case("bind", fail=2, detach=1)
        self.assertEqual(json.loads(result.stdout)["error"], "bind_rollback_failed")

    def test_readback_failure_detaches_owned_bind(self):
        result = self.run_case("bind", after=0)
        self.assertEqual(json.loads(result.stdout)["error"], "bind_readback_failed")
        self.assertEqual(result.stderr.strip(), "3 1 10 2")

    def test_detach_and_restore_readback(self):
        result = self.run_case("detach", before=1, after=0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["state"], "native")
        self.assertEqual(result.stderr.strip(), "0 1 10 2")
        result = self.run_case("detach", before=1, after=1)
        self.assertEqual(json.loads(result.stdout)["error"], "original_not_restored")

    def test_unowned_target_never_mounts(self):
        result = self.run_case("bind", before=2)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "0 0 0 1")

    def test_partial_writable_bind_can_be_restored_or_repaired(self):
        status = self.run_case("status", before=1, count=1)
        self.assertEqual(json.loads(status.stdout)["error"], "target_not_readonly")
        restored = self.run_case("detach", before=1, after=0, count=1)
        self.assertEqual(restored.returncode, 0)
        self.assertEqual(json.loads(restored.stdout)["state"], "native")
        repaired = self.run_case("bind", before=1, after=1, count=1)
        self.assertEqual(repaired.returncode, 0)
        self.assertEqual(json.loads(repaired.stdout)["state"], "bound")
        self.assertEqual(repaired.stderr.strip(), "2 0 0 2")


if __name__ == "__main__":
    unittest.main()
