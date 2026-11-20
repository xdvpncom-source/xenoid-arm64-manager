"""Execute the actual Native collector and actual guest awk against large tables."""
import ast
import base64
import gzip
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import types
import unittest
from typing import Optional

from test_proxy_observer_classification import patched_observer
SOURCE='shipped-native-backend-patch'
PATCHED_SOURCE=patched_observer()
METHOD='_rootd_port_inodes'
def load(source):
 node=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name==METHOD)
 ns={'Optional':Optional};exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-native-port-collector>','exec'),ns);return ns[METHOD]
OLD_METHOD=gzip.decompress(base64.b64decode('H4sIAAAAAAAC/5VTTY/TMBC98yuGcElWIW2gWkREV9rLHuHCASmKKjeZgIXXtuyJhIT474zjJCWkPexc/PX85s0bu8MeTs4Y6k7WODpJbTr06SuYwqPq82V1d5l2KDolNVbwxZI0Wqi6V0ZQA0f4bDRGZAZvH4AGq7D2SLUn1+RwNkY11cKEvyy2hFEA3+6NexaUhsyFQuGxuOjLIdkfviXZclkZY8+i/cn3fi+bIZJ9uef48JTk/+3fijIO/+D/LLNoSwVzEZyOp+lFR2t0r2Qb9D8J5XE54GrACvrBFJAmO+tMu9NIO2ptwtWsNu6TrFqJNQPZgcZkbMbUp7iZroAh6qQVxJwhW5NvjpV8lnS8P8AdlPt3hy1g7uhxnqwh2Wol+1mc9GO/qw2fQxqcjj7l8NUNCPAGHjUM2nEGcVbIojyhRgc0LplLGwIBvUOE0PBiRRvMDMqCmTF94a2SFPZ8mtVl1Wx19BJV59nEgIr4NNuguCCFOo3gDD6xScDZ4rp+38DrI7+dx2TLP7WfpB5wcyi6zqH3OdtghRNkXA7zO4/UZVM4PiIZflGaVMlVbcGWhSIIG80ZrEWXZkHb6he9UCTzTzoXxpCPTZ6/103C+dGH9r4wafhSFxs+NtdkjaBC+k5+D127LiN+zoJLSMfpxcCpqRGQwQOUa4ZbBUxPN97LF9hfzgYXZC0FAAA=')).decode()
assert hashlib.sha256(OLD_METHOD.encode()).hexdigest()=='a28d2ba97d74b0a621ecdc59b97764db71aad67a40a7bc99a9234a1ea10a8f90'
node=next(n for n in ast.walk(ast.parse(PATCHED_SOURCE)) if isinstance(n,ast.FunctionDef) and n.name==METHOD)
assert hashlib.sha256(ast.get_source_segment(PATCHED_SOURCE,node).encode()).hexdigest()=='8a2e59773d73b54bc38ebb15112c66a1c39e0c8f67ae8c2d668164a3159a11b4'
collect=load(PATCHED_SOURCE);old_collect=load(OLD_METHOD)
HEADER='  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n'
def row(address='0100007F',port='494F',state='0A',inode='4950291',remote='00000000:0000'):
 return f'   0: {address}:{port} {remote} {state} 00000000:00000000 00:00000000 00000000 0 0 {inode} 1 0000000000000000 100 0 0 10 0\n'
class Fixture:
 def __init__(self,root,tcp=HEADER,tcp6=HEADER):
  self.root=root;self.lease=types.SimpleNamespace(rootd_port=18767);self.files={'/proc/net/tcp':root/'tcp','/proc/net/tcp6':root/'tcp6'};self.files['/proc/net/tcp'].write_text(tcp);self.files['/proc/net/tcp6'].write_text(tcp6);self.calls=[];self.output_sizes=[];self.failed=None;self.awk_failure=False
 def _rootd_output(self,args,*,limit,deadline):
  self.calls.append((list(args),limit,deadline));path=args[-1]
  if self.failed==path:return None
  command=[*args[:-1],str(self.files[path])]
  if self.awk_failure and args[0]=='awk':command=['awk','broken { syntax',str(self.files[path])]
  result=subprocess.run(command,capture_output=True);self.output_sizes.append(len(result.stdout))
  return result.stdout.decode() if result.returncode==0 and len(result.stdout)<=limit else None
class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def fixture(self,*args,**kw):return Fixture(self.root,*args,**kw)
 def test_large_unrelated_tables_keep_exact_one_local_rootd_listener(self):
  bulk=HEADER+''.join(row(port='1234',state='01',inode=str(i)) for i in range(700));tcp=bulk+row();self.assertGreater(len(tcp.encode()),65536)
  f=self.fixture(tcp);self.assertEqual(old_collect(f,deadline=123),(set(),True));f.calls.clear();f.output_sizes.clear()
  self.assertEqual(collect(f,deadline=123),({'4950291'},False));self.assertTrue(all(size<1024 for size in f.output_sizes));self.assertEqual(len(f.calls),2);self.assertTrue(all(limit==65536 and deadline==123 for _,limit,deadline in f.calls))
 def test_first_matching_row_is_not_lost_when_header_is_preserved(self):
  f=self.fixture(HEADER+row());self.assertEqual(collect(f),({'4950291'},False));args=f.calls[0][0];out=subprocess.check_output([*args[:-1],str(f.files[args[-1]])]).decode();self.assertTrue(out.startswith(HEADER));self.assertEqual(len(out.splitlines()),2)
 def test_no_match_keeps_both_family_headers_and_reports_no_listener(self):
  f=self.fixture(HEADER+row(port='1234'),HEADER+row(address='00000000000000000000000001000000',port='1234'));self.assertEqual(collect(f),(set(),False));self.assertEqual(len(f.calls),2)
 def test_family_read_failure_is_conflict_even_if_other_family_is_valid(self):
  for failed in ('/proc/net/tcp','/proc/net/tcp6'):
   with self.subTest(failed=failed):
    f=self.fixture(HEADER+row());f.failed=failed;self.assertEqual(collect(f),(set(),True))
 def test_actual_awk_error_is_conflict_not_empty_table(self):
  f=self.fixture(HEADER+row());f.awk_failure=True;self.assertEqual(collect(f),(set(),True))
 def test_actual_file_open_failure_is_conflict(self):
  f=self.fixture();f.files['/proc/net/tcp6'].unlink();self.assertEqual(collect(f),(set(),True))
 def test_any_address_or_foreign_local_address_still_conflicts(self):
  for address in ('00000000','0200007F','00000000000000000000000000000000'):
   with self.subTest(address=address):self.assertEqual(collect(self.fixture(HEADER+row(address=address))),(set(),True))
 def test_distinct_listener_inodes_on_same_port_still_conflict(self):
  self.assertEqual(collect(self.fixture(HEADER+row()+row(inode='777'))),({'4950291','777'},True))
 def test_dual_family_distinct_listeners_conflict(self):
  f=self.fixture(HEADER+row(),HEADER+row(address='00000000000000000000000001000000',inode='777'));self.assertEqual(collect(f),({'4950291','777'},True))
 def test_ipv6_loopback_listener_uses_original_field_indices(self):
  f=self.fixture(HEADER,HEADER+row(address='00000000000000000000000001000000'));self.assertEqual(collect(f),({'4950291'},False))
 def test_remote_port_match_is_not_a_local_listener_match(self):
  f=self.fixture(HEADER+row(port='1234',remote='0100007F:494F'));self.assertEqual(collect(f),(set(),False))
 def test_same_local_port_established_connection_is_not_listening(self):
  self.assertEqual(collect(self.fixture(HEADER+row(state='01'))),(set(),False))
 def test_case_normalization_and_duplicate_inode_behavior_match_original(self):
  tcp=HEADER+row(port='494f')+row(port='494f');f=self.fixture(tcp);self.assertEqual(collect(f),old_collect(f));self.assertEqual(collect(f),({'4950291'},False))
 def test_malformed_row_and_inode_handling_match_original_parser(self):
  for payload in ('broken\n',row(inode='not_numeric'),row(state='0a'),'0: broken:494F 0 0A short\n'):
   with self.subTest(payload=payload):
    f=self.fixture(HEADER+payload);self.assertEqual(collect(f),old_collect(f))
 def test_many_matching_listeners_still_respect_capture_limit_fail_closed(self):
  f=self.fixture(HEADER+''.join(row(inode=str(i)) for i in range(700)));self.assertEqual(collect(f),(set(),True));self.assertGreater(f.output_sizes[0],65536)
 def test_invalid_lease_never_reports_free_port_or_reads_tables(self):
  for port in (None,True,'18767',-1,0,65536):
   with self.subTest(port=port):
    f=self.fixture();f.lease.rootd_port=port;self.assertEqual(collect(f),(set(),True));self.assertFalse(f.calls)
 def test_valid_boundary_ports_are_exact_and_not_shell_interpolated(self):
  for port in (1,65535):
   with self.subTest(port=port):
    f=self.fixture(HEADER+row(port=format(port,'04X')));f.lease.rootd_port=port;self.assertEqual(collect(f),({'4950291'},False));self.assertEqual(f.calls[0][0][:3],['awk','-v','port=:'+format(port,'04X')])
if __name__=='__main__':unittest.main()
