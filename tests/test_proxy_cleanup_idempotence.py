"""Actual Native absence readback for owned, namespaced proxy cleanup."""
import runpy
import shutil
import subprocess
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import test_proxy_domain_routing as routing

TEMP=tempfile.TemporaryDirectory(prefix='xenoid-cleanup-tests-')
ROOT=Path(TEMP.name);(ROOT/'scripts').mkdir()
shutil.copyfile(routing.CANDIDATE/'scripts/xenoid-proxy-engine.py',ROOT/'scripts/xenoid-proxy-engine.py')
subprocess.run(['git','apply',str(routing.ROOT/'patches/proxy-namespaced-cleanup.patch')],cwd=ROOT,check=True,capture_output=True)
ENGINE=runpy.run_path(str(ROOT/'scripts/xenoid-proxy-engine.py'))
GLOBALS=ENGINE['_ip_absent'].__globals__
PREFIX=['/usr/bin/nsenter','--net=/run/netns/xenoid-p-fixture','--']
RULE=['/usr/sbin/ip','-4','rule','del','priority','30012','fwmark','0xa0100300/0xffffff00','lookup','20012']
ROUTE=['/usr/sbin/ip','-6','route','del','table','20012','default','dev','fixture0']

def result(code=0,out=b'',err=b''):return types.SimpleNamespace(returncode=code,stdout=out,stderr=err)


class NamespaceCleanupTests(unittest.TestCase):
    def call(self,command,response,exists=True):
        calls=[]
        def run(argv,**kwargs):calls.append((argv,kwargs));return response
        with patch.dict(GLOBALS,{'run':run}),patch.object(Path,'exists',return_value=exists):answer=ENGINE['_ip_absent'](command)
        return answer,calls

    def test_existing_namespace_absent_rule_uses_same_namespace_readback(self):
        answer,calls=self.call(PREFIX+RULE,result(out=b'0: from all lookup local\n32766: from all lookup main\n'))
        self.assertTrue(answer);self.assertEqual(calls[0][0],PREFIX+['/usr/sbin/ip','-4','rule','show']);self.assertFalse(calls[0][1]['check'])

    def test_existing_owned_rule_is_not_claimed_absent(self):
        answer,_=self.call(PREFIX+RULE,result(out=b'30012: from all fwmark 0xa0100300/0xffffff00 lookup 20012\n'))
        self.assertFalse(answer)

    def test_missing_namespace_needs_no_inner_command(self):
        answer,calls=self.call(PREFIX+RULE,result(),exists=False)
        self.assertTrue(answer);self.assertEqual(calls,[])

    def test_rule_permission_or_transport_error_is_not_absence(self):
        for error in (b'RTNETLINK answers: Operation not permitted\n',b'',b'nsenter: reassociate failed: Invalid argument\n'):
            with self.subTest(error=error):answer,_=self.call(PREFIX+RULE,result(2,err=error));self.assertFalse(answer)

    def test_route_absence_and_known_missing_fib_are_bounded(self):
        for reply in (result(),result(2,err=b'Error: ipv6: FIB table does not exist.\nDump terminated\n')):
            answer,calls=self.call(PREFIX+ROUTE,reply);self.assertTrue(answer);self.assertEqual(calls[0][0],PREFIX+['/usr/sbin/ip','-6','route','show','table','20012'])
        for reply in (result(out=b'default dev fixture0\n'),result(2,err=b'Operation not permitted\n'),result(1,err=b'unknown error\n')):
            self.assertFalse(self.call(PREFIX+ROUTE,reply)[0])

    def test_missing_link_requires_explicit_absence_not_just_nonzero_exit(self):
        command=PREFIX+['/usr/sbin/ip','link','del','dev','fixture0']
        self.assertTrue(self.call(command,result(1,err=b'Device "fixture0" does not exist.\n'))[0])
        self.assertFalse(self.call(command,result(1,err=b'Operation not permitted\n'))[0])
        self.assertFalse(self.call(command,result())[0])

    def test_native_remove_rejects_real_failure_but_accepts_already_absent_owned_rule(self):
        for absence in (True,False):
            calls=[]
            def run(argv,**kwargs):
                calls.append(argv)
                return result(2,err=b'No such file or directory')if 'del'in argv else result(out=b''if absence else b'30012: from all lookup 20012\n')
            with self.subTest(absence=absence),patch.dict(GLOBALS,{'run':run}),patch.object(Path,'exists',return_value=True):
                if absence:ENGINE['remove_command_resource']({'delete':PREFIX+RULE})
                else:
                    with self.assertRaises(ENGINE['Error'])as error:ENGINE['remove_command_resource']({'delete':PREFIX+RULE})
                    self.assertEqual(error.exception.code,'cleanup_incomplete')
            self.assertEqual(len(calls),2)

    def test_local_ip_rule_readback_remains_supported(self):
        answer,calls=self.call(RULE,result())
        self.assertTrue(answer);self.assertEqual(calls[0][0],['/usr/sbin/ip','-4','rule','show'])


if __name__=='__main__':unittest.main()
