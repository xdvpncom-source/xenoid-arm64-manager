"""Actual helper/held-FD/netlink transaction branches; private temporary FS only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('control_routes_candidate', Path(__file__).resolve().parents[1]/'qa/qa_control_routes.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
CID='a'*64; IID='11111111-2222-4333-8444-555555555555'


class Controls(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.proc=root/'proc';process=self.proc/'100';(process/'ns').mkdir(parents=True)
        (process/'ns/net').write_text('owned-netns');(process/'uid_map').write_text('0 0 4294967295')
        self.stat(process/'stat',123)
        guest=process/'root/proc';(guest/'net').mkdir(parents=True);(process/'net').symlink_to(guest/'net');adbd=guest/'1570';(adbd/'fd').mkdir(parents=True)
        self.stat(adbd/'stat',456);(adbd/'comm').write_text('adbd');(adbd/'status').write_text('Uid:\t2000\t2000\t2000\t2000\n')
        (adbd/'exe').symlink_to('/apex/com.android.adbd/bin/adbd');(adbd/'fd/3').symlink_to('socket:[777]')
        self.listener(guest,2000)
        self.lease=types.SimpleNamespace(network_name='fixture-net',ipv4_address='172.31.0.10',ipv4_gateway='172.31.0.1',
            ipv4_subnet='172.31.0.0/28',ipv6_address='fd78:656e:6f69::10',ipv6_gateway='fd78:656e:6f69::1',
            ipv6_subnet='fd78:656e:6f69::/64',mac_address='02:00:00:00:00:01',android_adb_port=62111)
        self.endpoint={'NetworkID':'network','IPAddress':self.lease.ipv4_address,'Gateway':self.lease.ipv4_gateway,
            'GlobalIPv6Address':self.lease.ipv6_address,'IPv6Gateway':self.lease.ipv6_gateway,'MacAddress':self.lease.mac_address}
        self.owned={'Id':CID,'Image':'image','State':{'Running':True,'Pid':100,'StartedAt':'fixture'},
                    'NetworkSettings':{'Networks':{'fixture-net':self.endpoint}}}
        self.manager=types.SimpleNamespace(context=types.SimpleNamespace(instance_id=IID,resource_tag='abcdef123456'),lease=self.lease,
            _owned_container_record=lambda **kw:(copy.deepcopy(self.owned),None),
            _inspect_docker_object=lambda *a,**kw:({'Id':'network'},None),_network_matches_lease=lambda n:n=={'Id':'network'})
        self.rows=[{'priority':0,'src':'all','table':'local'},{'priority':13000,'table':'1268','iif':'lo','fwmark':'0'}]
        self.commands=[];self.mutations=[];self.after_add=None;self.link_missing=False;self.bad_route=False;self.fail_after_add=False
        self.state=root/'state';self.journal=self.state/IID/(CID+'-123.json')
        self.patches=[patch.object(c,'STATE',self.state),patch.object(c,'PROC',self.proc),patch.object(c,'OWNER',os.getuid()),
            patch.object(c.os,'geteuid',return_value=0),patch.object(c.subprocess,'run',side_effect=self.run_ip)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    @staticmethod
    def stat(path,start):path.write_text('1 (fixture) S '+' '.join(['0']*18+[str(start)]+['0']*5))
    @staticmethod
    def listener(guest,uid):
        (guest/'net/tcp').write_text('header\n')
        (guest/'net/tcp6').write_text('header\n 0: '+('0'*32)+':F29F '+('0'*32)+':0000 0A 0 0 0 '+str(uid)+' 0 777\n')
    def rule(self):return {'priority':900,'src':'172.31.0.10/32','dst':'172.31.0.1/32','iif':'lo',
                           'ipproto':'tcp','sport':62111,'uid_start':2000,'uid_end':2000,'table':1002}
    def run_ip(self,argv,**options):
        self.commands.append((argv,options));self.assertNotIn('--target',argv)
        fd=options['pass_fds'][0];self.assertTrue(os.fstat(fd).st_ino)
        self.assertIn('--net=/proc/self/fd/'+str(fd),argv)
        args=argv[argv.index('--')+2:];code=0
        if 'address' in args:
            body=[]if self.link_missing else[{'ifname':'rmnet_data0','ifindex':2,'address':self.lease.mac_address,
                'addr_info':[{'family':'inet','local':self.lease.ipv4_address,'prefixlen':28}]}]
        elif 'route' in args:
            body=[]if self.bad_route else[{'dst':self.lease.ipv4_subnet,'dev':'rmnet_data0','scope':'link'},
                {'dst':'default','dev':'rmnet_data0','gateway':self.lease.ipv4_gateway}]
        elif 'show' in args:body=copy.deepcopy(self.rows)
        else:
            self.assertEqual(args[:2],['-4','rule']);self.assertNotIn('flush',args)
            self.assertEqual(c.private_read(self.journal)['phase'],'pending'if args[2]=='add'else c.private_read(self.journal)['phase'])
            self.mutations.append(args);body=None
            if args[2]=='add':
                self.rows.append(self.rule())
                if self.after_add:self.after_add()
                if self.fail_after_add:code=1
            elif args[2]=='del':self.rows=[r for r in self.rows if r!=self.rule()]
            else:self.fail('unexpected mutation')
        return types.SimpleNamespace(returncode=code,stdout=b''if body is None else json.dumps(body).encode(),stderr=b'')
    def ensure(self):return c.ensure_control_routes(self.manager,CID)
    def test_fsynced_intent_then_exact_add_readback_idempotent_restore(self):
        original=copy.deepcopy(self.rows)
        with patch.object(c.os,'fsync',wraps=os.fsync)as sync:
            self.assertTrue(self.ensure()['changed']);self.assertGreaterEqual(sync.call_count,4)
        self.assertEqual(c.private_read(self.journal)['phase'],'committed')
        self.assertFalse(self.ensure()['changed']);self.assertEqual(len(self.mutations),1)
        self.assertTrue(c.restore_control_routes(self.manager,CID)['restored']);self.assertEqual(self.rows,original)
        self.assertEqual(c.private_read(self.journal)['phase'],'restored')
    def test_foreign_or_unowned_identical_priority_rejected_without_mutation(self):
        for row in({'priority':900,'table':'main'},self.rule()):
            self.rows=[{'priority':0,'table':'local'},row]
            with self.assertRaises(RuntimeError):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_post_add_unknown_ack_rolls_back_only_exact_created_rule(self):
        self.fail_after_add=True
        with self.assertRaisesRegex(RuntimeError,'netlink_failed'):self.ensure()
        self.assertEqual([r[2]for r in self.mutations],['add','del'])
        self.assertEqual(c.private_read(self.journal)['phase'],'restored')
    def test_binding_race_after_add_preserves_unresolved_intent_never_touches_new_pid(self):
        self.after_add=lambda:self.owned['State'].update(Pid=101)
        with self.assertRaisesRegex(RuntimeError,'binding_changed'):self.ensure()
        self.assertEqual([r[2]for r in self.mutations],['add'])
        self.assertEqual(c.private_read(self.journal)['phase'],'rollback_pending')
    def test_foreign_rule_racing_readback_is_preserved_and_blocks_retry(self):
        self.after_add=lambda:self.rows.append({'priority':900,'table':'foreign'})
        with self.assertRaisesRegex(RuntimeError,'add_readback_failed'):self.ensure()
        self.assertEqual(len(self.rows),4);self.assertEqual(c.private_read(self.journal)['phase'],'rollback_pending')
        with self.assertRaises(RuntimeError):self.ensure()
        self.assertEqual(len(self.mutations),1)
    def test_pid_reuse_before_write_rejected(self):
        original=self.manager._owned_container_record;calls=[]
        def read(**kw):
            calls.append(1)
            if len(calls)==2:self.stat(self.proc/'100/stat',999)
            return original(**kw)
        self.manager._owned_container_record=read
        with self.assertRaisesRegex(RuntimeError,'binding_changed'):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_socket_uid_or_process_fd_owner_unproven_rejects(self):
        guest=self.proc/'100/root/proc';self.listener(guest,10020)
        with self.assertRaisesRegex(RuntimeError,'socket_uid_invalid'):self.ensure()
        self.listener(guest,2000);(guest/'1570/fd/3').unlink()
        with self.assertRaisesRegex(RuntimeError,'socket_owner_unproven'):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_missing_physical_iface_or_connected_route_rejects(self):
        self.link_missing=True
        with self.assertRaisesRegex(RuntimeError,'physical_interface_missing'):self.ensure()
        self.link_missing=False;self.bad_route=True
        with self.assertRaisesRegex(RuntimeError,'physical_routes_unproven'):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_public_destination_or_wrong_endpoint_never_accepted(self):
        self.lease.ipv4_gateway='8.8.8.8';self.endpoint['Gateway']='8.8.8.8'
        with self.assertRaisesRegex(RuntimeError,'nonprivate_lease'):self.ensure()
        self.lease.ipv4_gateway='172.31.0.1'
        with self.assertRaisesRegex(RuntimeError,'network_endpoint_mismatch'):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_only_ipv4_own_source_gateway_lease_adb_port_no_extra_daemon_or_tun_write(self):
        self.ensure();argv=self.mutations[0]
        self.assertEqual(argv,['-4','rule','add','pref','900','from','172.31.0.10/32','to','172.31.0.1/32',
            'iif','lo','ipproto','tcp','sport','62111','uidrange','2000-2000','lookup','1002'])
        self.assertTrue(all('-6'not in args for args,_ in self.commands));self.assertNotIn('18765',argv);self.assertNotIn('tun0',argv)
    def test_unrelated_fd_disappears_but_live_listener_proof_survives(self):
        extra=self.proc/'100/root/proc/1570/fd/9';extra.symlink_to('socket:[999]');real=os.readlink
        def read(path):
            if Path(path)==extra:raise FileNotFoundError('accepted_client_closed')
            return real(path)
        with patch.object(c.os,'readlink',side_effect=read):self.assertTrue(self.ensure()['ok'])
    def test_listener_fd_disappears_or_fd_permission_fails_is_not_zero_or_success(self):
        listener=self.proc/'100/root/proc/1570/fd/3';real=os.readlink
        def gone(path):
            if Path(path)==listener:raise FileNotFoundError('listener_closed')
            return real(path)
        with patch.object(c.os,'readlink',side_effect=gone):
            with self.assertRaisesRegex(RuntimeError,'socket_owner_unproven'):self.ensure()
        def denied(path):
            if Path(path)==listener:raise PermissionError('unreadable_listener')
            return real(path)
        with patch.object(c.os,'readlink',side_effect=denied):
            with self.assertRaises(PermissionError):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_bad_uid_map_or_earlier_rule_rejects(self):
        (self.proc/'100/uid_map').write_text('0 100000 65536')
        with self.assertRaisesRegex(RuntimeError,'uid_map_unproven'):self.ensure()
        (self.proc/'100/uid_map').write_text('0 0 4294967295');self.rows.append({'priority':899,'table':1})
        with self.assertRaisesRegex(RuntimeError,'earlier_rule_conflict'):self.ensure()
    def test_intent_persistence_failure_never_adds_rule(self):
        with patch.object(c,'save',side_effect=OSError('fixture_disk_full')):
            with self.assertRaises(OSError):self.ensure()
        self.assertEqual(self.mutations,[])
    def test_success_ack_without_rule_is_failed_and_restored(self):
        self.after_add=lambda:self.rows.pop()
        with self.assertRaisesRegex(RuntimeError,'add_readback_failed'):self.ensure()
        self.assertEqual(c.private_read(self.journal)['phase'],'restored')
    def test_actual_iproute2_normalization_rejects_extra_selectors(self):
        row={**self.rule(),'src':'172.31.0.10','dst':'172.31.0.1','table':'1002'}
        self.assertEqual(c.normalized_rule(row),self.rule())
        self.assertIsNone(c.normalized_rule({**row,'fwmark':'0x0'}))
        self.assertIsNone(c.normalized_rule({**row,'sport':{'start':62111,'end':62112}}))
    def test_record_symlink_hardlink_and_bad_binding_refuse_restore(self):
        self.ensure();body=self.journal.read_bytes();self.journal.unlink();target=self.journal.with_suffix('.other');target.write_bytes(body);self.journal.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError,'unsafe_record'):c.restore_control_routes(self.manager,CID)
        self.journal.unlink();self.journal.write_bytes(body);self.journal.chmod(0o600);target.unlink();data=json.loads(self.journal.read_bytes());data['binding']['image']='different';c.save(self.journal,data)
        with self.assertRaisesRegex(RuntimeError,'record_binding_mismatch'):c.restore_control_routes(self.manager,CID)


if __name__=='__main__':unittest.main()
