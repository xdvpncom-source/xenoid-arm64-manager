"""Actual shell-link recovery tests plus strict official-input evidence checks.

The shell runs in a local filesystem sandbox. /proc, Android property/syscall
queries and char-device stat are mocked; hardlinks/unlinks/restart recovery are
real OS operations. No device input or production changes occur.
"""
import base64
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qa_input_ready', ROOT/'qa/qa_input_ready.py')
q = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = q
spec.loader.exec_module(q)

PROFILE = {'input': {'name': 'sec_touchscreen', 'x': {'minimum': 0, 'maximum': 1439}, 'y': {'minimum': 0, 'maximum': 3119}}}
NATIVE = {'ok': True, 'schema': 'dev.input/v2', 'name': 'sec_touchscreen', 'xMin': 0, 'xMax': 1439, 'yMin': 0, 'yMax': 3119, 'driverLayer': True, 'directTouch': True, 'persistentDevice': True, 'eventNode': '/dev/input/event3'}
STATUS = json.dumps(NATIVE, separators=(',', ':'))
KERNEL = '''  name:     "sec_touchscreen"
    ABS_X : value 0, min 0, max 1439, fuzz 0
    ABS_Y : value 0, min 0, max 3119, fuzz 0
    ABS_MT_POSITION_X : value 0, min 0, max 1439, fuzz 0
    ABS_MT_POSITION_Y : value 0, min 0, max 3119, fuzz 0
  input props:
    INPUT_PROP_DIRECT
'''


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def evidence():
    return {'STATUS': STATUS, 'PID':'20', 'START':'8142202', 'NODE':'/dev/input/event3', 'ALIAS':'/dev/input/.xenoid-reannounce-20-8142202-event3', 'UINPUT_FD':'3', 'SOCKET_FD':'4', 'SOCKET_INODE':'4831484', 'META':'60:604:21b0:d:43:0:1004:660', 'PROFILE_SHA':'a'*64, 'SERVER_SHA':'b'*64, 'CLIENT_SHA':'b'*64, 'SYSFS_NAME':base64.b64encode(b'sec_touchscreen\n').decode(), 'SYSFS_DEV':'13:67', 'PROPERTIES':'2', 'GETEVENT':base64.b64encode(KERNEL.encode()).decode()}


def records(value):
    return ''.join(f'{k}\t{v}\n' for k,v in value.items())


class NativeInputEvidenceTests(unittest.TestCase):
    def test_valid_official_service_kernel_and_effective_profile(self):
        b = q.parse_probe(records(evidence()), PROFILE)
        self.assertEqual(b.node, '/dev/input/event3')
        self.assertEqual(b.metadata, '60:604:21b0:d:43:0:1004:660')

    def assert_reject(self, field, value):
        e = evidence(); e[field] = value
        with self.assertRaises(q.NativeInputBindingError): q.parse_probe(records(e), PROFILE)

    def test_wrong_binary_rejected(self): self.assert_reject('SERVER_SHA', 'c'*64)
    def test_wrong_owner_rejected(self): self.assert_reject('META', '60:604:21b0:d:43:1000:1004:660')
    def test_wrong_group_rejected(self): self.assert_reject('META', '60:604:21b0:d:43:0:0:660')
    def test_wrong_mode_rejected(self): self.assert_reject('META', '60:604:2180:d:43:0:1004:600')
    def test_noncharacter_node_rejected(self): self.assert_reject('META', '60:604:81b0:d:43:0:1004:660')
    def test_wrong_sysfs_device_rejected(self): self.assert_reject('SYSFS_DEV', '13:68')
    def test_no_direct_property_rejected(self): self.assert_reject('PROPERTIES', '0')
    def test_path_injection_rejected(self): self.assert_reject('NODE', '/dev/input/event3;touch /tmp/wrong')
    def test_changed_alias_binding_rejected(self): self.assert_reject('ALIAS', '/dev/input/.other')
    def test_same_uinput_and_socket_fd_rejected(self): self.assert_reject('SOCKET_FD','3')
    def test_changed_kernel_axis_rejected(self): self.assert_reject('GETEVENT',base64.b64encode(KERNEL.replace('max 1439','max 719').encode()).decode())
    def test_duplicated_kernel_axis_rejected(self): self.assert_reject('GETEVENT',base64.b64encode((KERNEL+'    ABS_X : value 0, min 0, max 1439, fuzz 0\n').encode()).decode())
    def test_cached_status_false_rejected(self):
        native = dict(NATIVE, ok=False)
        self.assert_reject('STATUS', json.dumps(native))
    def test_duplicate_probe_field_rejected(self):
        with self.assertRaises(q.NativeInputBindingError): q.parse_probe(records(evidence())+'PID\t20\n',PROFILE)
    def test_exact_rootd_size_limit_and_shell_syntax(self):
        b=q.parse_probe(records(evidence()),PROFILE)
        for script in (q.probe_command(),q.repair_command(b)):
            self.assertLessEqual(len(script.encode()),4096)
            subprocess.run(['sh','-n'],input=script,text=True,check=True,capture_output=True)
    def test_repair_reply_does_not_claim_reader_ready(self):
        b=q.parse_probe(records(evidence()),PROFILE)
        reply={'ok': True,'schema':'xenoid.native-input-reannouncement/v1','reannounced': True,'same_inode':True,'driver_recreated':False,'events_injected':False,'binding_digest':b.digest}
        self.assertEqual(q.parse_repair(json.dumps(reply),b),reply)
        with self.assertRaises(q.NativeInputBindingError): q.parse_repair(json.dumps(dict(reply,events_injected=True)),b)
    def test_tampered_repair_binding_rejected(self):
        b=q.parse_probe(records(evidence()),PROFILE)
        with self.assertRaises(q.NativeInputBindingError): q.repair_command(replace(b,alias_dir='/tmp/elsewhere'))


class RealLinkSandbox:
    def __init__(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-input-reannounce-')
        self.root=Path(self.temp.name)
        self.bin=self.root/'bin';self.bin.mkdir()
        self.node=self.root/'dev/input/event3';self.node.parent.mkdir(parents=True)
        self.node.write_text('officialevent');self.node.chmod(0o660)
        self.original_inode=self.node.stat().st_ino
        self.alias=self.node.parent/'.xenoid-reannounce-20-8142202-event3'
        self.environment=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],SANDBOX=str(self.root))
        self.create('data/local/tmp/xenoid-profile/effective.json',json.dumps(PROFILE))
        server='#!/bin/sh\nprintf \'%s\\n\' '+repr(STATUS)+'\n'
        self.create('system/bin/xenoid-input',server,0o755)
        self.create('data/local/tmp/xenoid-input',server,0o755)
        self.create('proc/20/comm','xenoid-input\n')
        self.create('proc/20/stat','20 (xenoid-input) S 1 '+'0 '*17+'8142202\n')
        self.create('proc/20/status','Uid:\t0\t0\t0\t0\n')
        self.create('proc/20/cmdline',str(self.root/'system/bin/xenoid-input')+'\0serve\0')
        self.create('proc/net/unix','ffff: 0 0 0 0001 01 4831484 '+str(self.root/'dev/socket/.inputd')+'\n')
        self.create('sys/class/input/event3/device/name','sec_touchscreen\n')
        self.create('sys/class/input/event3/device/properties','2\n')
        self.create('sys/class/input/event3/dev','13:67\n')
        self.symlink('proc/20/exe',self.root/'system/bin/xenoid-input')
        self.symlink('proc/20/fd/3',self.root/'dev/uinput')
        self.symlink('proc/20/fd/4','socket:[4831484]')
        self.wrapper('base64',r'''
import sys,base64
print(base64.b64encode(open(sys.argv[1],'rb').read() if len(sys.argv)>1 else sys.stdin.buffer.read()).decode())
''')
        self.wrapper('getprop',r'''
import sys
print('running' if sys.argv[1]=='init.svc.xenoid-input' else '20')
''')
        self.wrapper('getevent',r'''
import sys
print('add device 1: '+sys.argv[-1])
print(KERNEL,end='')
'''.replace('KERNEL',repr(KERNEL)))
        self.wrapper('stat',r'''
import os,sys,stat
fmt,path=sys.argv[2:]; s=os.stat(path)
if fmt=='%u:%g:%a': print('0:0:'+oct(stat.S_IMODE(s.st_mode))[2:])
elif fmt=='%d:%i:%f:%t:%T:%u:%g:%a':
 content=open(path).read();minor=67 if content=='officialevent' else 68
 print(f'{s.st_dev}:{s.st_ino}:{(stat.S_IFCHR|stat.S_IMODE(s.st_mode)):x}:d:{minor:x}:0:1004:{stat.S_IMODE(s.st_mode):o}')
else: raise SystemExit(2)
''')
        self.wrapper('ln',r'''
import os,sys,signal
src,dst=sys.argv[1:]
if os.environ.get('INTERRUPT_LINK')=='1' and dst.endswith('/dev/input/event3'):
 os.kill(os.getppid(),signal.SIGKILL);raise SystemExit(2)
if os.environ.get('FAIL_LINK')=='1' and dst.endswith('/dev/input/event3'): raise SystemExit(2)
os.link(src,dst)
''')

    def create(self,path,text,mode=0o644):
        dest=self.root/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text);dest.chmod(mode);return dest
    def symlink(self,path,target):
        dest=self.root/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.symlink_to(target)
    def wrapper(self,name,code): self.create('bin/'+name,'#!'+sys.executable+'\n'+code,0o755)
    def command(self,script):
        for prefix in ('/dev/','/proc/','/sys/','/data/','/system/'):
            script=script.replace(prefix,str(self.root)+prefix)
        # No privileged char nodes are created in this sandbox. The real stat
        # probe mock independently supplies/validates char-mode and rdev.
        script=script.replace('; [ -n "$n" ]||f', '; n='+str(self.root)+'$n; [ -n "$n" ]||f')
        return script.replace('[ -c ','[ -f ')
    def run(self,script,**environment):
        return subprocess.run(['sh','-c',self.command(script)],text=True,capture_output=True,env=dict(self.environment,**environment),timeout=8)
    def probe(self):
        r=self.run(q.probe_command())
        if r.returncode: raise AssertionError((r.returncode,r.stdout,r.stderr))
        # Restore logical Android paths in the transport output only.
        stdout=r.stdout.replace(str(self.root),'')
        return q.parse_probe(stdout,PROFILE)
    def close(self): self.temp.cleanup()


class ActualShellLinkRecoveryTests(unittest.TestCase):
    def setUp(self):self.s=RealLinkSandbox()
    def tearDown(self):self.s.close()
    def test_normal_reannouncement_preserves_actual_inode_and_removes_alias(self):
        b=self.s.probe();r=self.s.run(q.repair_command(b))
        self.assertEqual(r.returncode,0,(r.stdout,r.stderr))
        q.parse_repair(r.stdout,b)
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
        self.assertFalse(self.s.alias.exists())
    def test_process_interruption_after_unlink_recovers_with_fresh_probe(self):
        b=self.s.probe();r=self.s.run(q.repair_command(b),INTERRUPT_LINK='1')
        self.assertNotEqual(r.returncode,0)
        self.assertFalse(self.s.node.exists())
        self.assertEqual((self.s.alias/'event').stat().st_ino,self.s.original_inode)
        again=self.s.probe()
        self.assertEqual(again,b)
        r=self.s.run(q.repair_command(again))
        self.assertEqual(r.returncode,0,(r.stdout,r.stderr))
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
        self.assertFalse(self.s.alias.exists())
    def test_persistent_link_failure_keeps_recovery_alias(self):
        b=self.s.probe();r=self.s.run(q.repair_command(b),FAIL_LINK='1')
        self.assertNotEqual(r.returncode,0)
        self.assertFalse(self.s.node.exists())
        self.assertTrue((self.s.alias/'event').exists())
        self.assertEqual(self.s.probe(),b)
    def test_concurrent_replacement_after_interruption_is_rejected(self):
        b=self.s.probe();self.s.run(q.repair_command(b),INTERRUPT_LINK='1')
        preserved=self.s.alias/'event'
        self.assertFalse(self.s.node.exists())
        self.s.node.write_text('officialevent');self.s.node.chmod(0o660)
        replacement_inode=self.s.node.stat().st_ino
        self.assertNotEqual(replacement_inode,preserved.stat().st_ino)
        fresh=self.s.probe()
        r=self.s.run(q.repair_command(fresh))
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.s.node.stat().st_ino,replacement_inode)
        self.assertEqual(preserved.stat().st_ino,self.s.original_inode)

    def test_changed_profile_is_rejected_before_unlink(self):
        b=self.s.probe();self.s.create('data/local/tmp/xenoid-profile/effective.json','{}')
        r=self.s.run(q.repair_command(b))
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
        self.assertFalse(self.s.alias.exists())
    def test_foreign_alias_inode_is_rejected_without_deleting_it(self):
        b=self.s.probe();self.s.alias.mkdir(mode=0o700)
        wrong=self.s.alias/'event';wrong.write_text('foreign');wrong.chmod(0o660)
        r=self.s.run(q.repair_command(b))
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(wrong.read_text(),'foreign')
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
    def test_symlink_alias_is_rejected(self):
        b=self.s.probe();other=self.s.root/'other';other.mkdir();self.s.alias.symlink_to(other)
        r=self.s.run(q.repair_command(b))
        self.assertNotEqual(r.returncode,0)
        self.assertTrue(self.s.alias.is_symlink())
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
    def test_changed_service_start_is_rejected_before_unlink(self):
        b=self.s.probe();self.s.create('proc/20/stat','20 (xenoid-input) S 1 '+'0 '*17+'999999\n')
        r=self.s.run(q.repair_command(b))
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)
    def test_changed_socket_is_rejected_before_unlink(self):
        b=self.s.probe();self.s.create('proc/net/unix','')
        r=self.s.run(q.repair_command(b))
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(self.s.node.stat().st_ino,self.s.original_inode)


if __name__=='__main__': unittest.main()
