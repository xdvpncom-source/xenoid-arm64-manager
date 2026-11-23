#!/usr/bin/env python3
"""Persist administrator domain policy and reconcile it through native Xenoid."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

UPSTREAM=Path('/opt/xenoid-fleet/upstream')
POLICY=Path('/etc/xenoid-proxy-routing/policy.json')
LEGACY_POLICY=Path('/etc/xenoid/qa-proxy-routing.json')
SCHEMA='dev.xenoid.qa.proxy-routing/v1'
DEFAULT={'schema':SCHEMA,'exceptions':[{'domain':domain,'include_subdomains':True}for domain in (
    'us.xdvpn.com','xdvpn.com','ir.xdgram.com','xdgram.com','github.com')],
    'failure_policy':{'schema':'dev.xenoid.proxy-failure-policy/v1','mode':'retry_proxy',
                      'retry_delays_seconds':[5,15,60]}}


class RoutingError(RuntimeError):
    pass


def require(value,code):
    if not value:raise RoutingError(code)


def canonical(value):
    return json.dumps(value,ensure_ascii=True,allow_nan=False,sort_keys=True,separators=(',',':')).encode()


def policy_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save_policy(value,*,path=POLICY,only_if_missing=False):
    from xenoid.proxy_source import MAX_SOURCE_BYTES,normalize_proxy_routing_policy,load_proxy_routing_policy
    value=normalize_proxy_routing_policy(value)
    body=canonical(value)+b'\n'
    require(len(body)<=MAX_SOURCE_BYTES,'routing_policy_too_large')
    parent=path.parent
    created=False
    try:
        parent.mkdir(parents=True,mode=0o755,exist_ok=False);created=True
    except FileExistsError:pass
    state=parent.lstat()
    if created:
        # Service UMask=0077 must not make this public policy unreadable to
        # the dropped agent UID. Never relax a pre-existing private directory.
        require(stat.S_ISDIR(state.st_mode)and state.st_uid==0 and not state.st_mode&0o022,'routing_policy_parent_invalid')
        descriptor=os.open(parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|os.O_NOFOLLOW)
        try:
            opened=os.fstat(descriptor)
            require((opened.st_dev,opened.st_ino)==(state.st_dev,state.st_ino)and opened.st_uid==0,'routing_policy_parent_changed')
            os.fchmod(descriptor,0o755);os.fsync(descriptor)
        finally:os.close(descriptor)
        state=parent.lstat()
    require(stat.S_ISDIR(state.st_mode)and state.st_uid==0 and not(stat.S_IMODE(state.st_mode)&0o022)and bool(stat.S_IMODE(state.st_mode)&0o001),'routing_policy_parent_invalid')
    if path.exists()or path.is_symlink():
        existing=load_proxy_routing_policy(path)
        if only_if_missing:return {'ok':True,'changed':False,'policy_sha256':policy_digest(existing),'exception_count':len(existing['exceptions'])}
        if existing==value:return {'ok':True,'changed':False,'policy_sha256':policy_digest(value),'exception_count':len(value['exceptions'])}
    descriptor,name=tempfile.mkstemp(prefix='.qa-proxy-routing.',dir=parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'wb')as stream:
            os.fchmod(stream.fileno(),0o644)
            os.fchown(stream.fileno(),0,0)
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        descriptor=os.open(parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:temporary.unlink(missing_ok=True)
    return {'ok':True,'changed':True,'policy_sha256':policy_digest(value),'exception_count':len(value['exceptions'])}


def read_legacy_policy(path):
    """Root-only migration from the old private Native settings directory.

    Never make that directory public. Only the normalized domain profile is
    copied to the dedicated public directory; the legacy file is retained.
    """
    from xenoid.proxy_source import MAX_SOURCE_BYTES,normalize_proxy_routing_policy
    require(os.geteuid()==0,'routing_root_required')
    path=Path(path);require(path.is_absolute()and '..'not in path.parts,'routing_legacy_path_invalid')
    parent=Path('/')
    for part in path.parts[1:-1]:
        parent/=part;info=parent.lstat()
        require(stat.S_ISDIR(info.st_mode)and info.st_uid==0 and not info.st_mode&0o022,'routing_legacy_parent_invalid')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|getattr(os,'O_CLOEXEC',0))
    with os.fdopen(descriptor,'rb')as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_nlink==1 and not info.st_mode&0o022 and info.st_size<=MAX_SOURCE_BYTES,'routing_legacy_file_invalid')
        body=stream.read(MAX_SOURCE_BYTES+1);require(len(body)<=MAX_SOURCE_BYTES,'routing_policy_too_large')
    return normalize_proxy_routing_policy(json.loads(body))


def ensure_default_policy(*,path=POLICY,legacy_path=LEGACY_POLICY):
    # An existing new custom/empty policy always wins. A private legacy policy
    # is consulted only by this explicit root migration, never by the agent.
    migrated=False;value=DEFAULT
    if not(path.exists()or path.is_symlink())and(legacy_path.exists()or legacy_path.is_symlink()):
        value=read_legacy_policy(legacy_path);migrated=True
    result=save_policy(value,path=path,only_if_missing=True)
    return {**result,'migrated_legacy':migrated and result['changed']}


def accepted(value):
    require(isinstance(value,dict)and value.get('ok')is True,
            (value.get('code')or value.get('error')or'routing_reconcile_pending')if isinstance(value,dict)else'routing_reconcile_pending')


def binding(manager):
    owned,_=manager._owned_container_record(timeout=10)
    require(owned is not None,'native_runtime_unavailable')
    return {'instance_id':manager.context.instance_id,'resource_tag':manager.context.resource_tag,'container_id':owned['Id'],
            'image_id':owned.get('Image'),'data':[row for row in owned.get('Mounts',[])if row.get('Destination')=='/data']},owned


def source(client):
    exported=client.proxy_export();accepted(exported)
    require(isinstance(exported.get('source'),dict),'proxy_source_unreadable')
    return exported['source']


def strict_proxy(manager,client,deadline):
    from xenoid.live_observe import LiveAcceptance,_Budget,_Checks
    status,bootstrap=client.proxy_status(),client.bootstrap_status()
    checks=_Checks()
    LiveAcceptance(manager)._proxy_checks(manager,{}, {'proxyGeneration':status.get('generation'),
        'runtimeEpoch':bootstrap.get('runtimeEpoch')},{},{'proxy':status,'bootstrap':bootstrap},_Budget(deadline),checks)
    require(checks.first_failure()is None,checks.first_failure()or'proxy_readiness_pending')


def active_config_matches(manager,policy):
    from xenoid.proxy_source import validate_proxy_routing_config
    try:
        manifest=manager._proxy_read_manifest(require_live=True,allow_absent=False)
        code,body=manager._proxy_root_process(['cat','--',manifest['paths']['config']],timeout=10,output_limit=10*1024*1024)
        if code:return False
        validate_proxy_routing_config(json.loads(body),policy)
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def reconcile(manager,broker,*,timeout,pending,proof=strict_proxy):
    from xenoid.proxy_source import load_proxy_routing_policy
    from xenoid.proxy_controller import ProxyController
    deadline=time.monotonic()+timeout if timeout else None
    policy=load_proxy_routing_policy();digest=policy_digest(policy)
    with manager.instance_operation_lock(timeout_seconds=timeout or None),broker.display_raw_docker(manager,deadline):
        before,owned=binding(manager)
        if (owned.get('State')or{}).get('Running')is not True:return {'ok':True,'skipped':'stopped','policy_sha256':digest}
        require(not pending(manager),'native_regeneration_pending')
        client=manager.daemon_client()
        status=client.proxy_status();accepted(status)
        require(status.get('stateReadable')is True and type(status.get('configured'))is bool and type(status.get('enabled'))is bool,'proxy_state_unreadable')
        if status.get('configured')is not True or status.get('enabled')is not True:
            return {'ok':True,'skipped':'disabled_or_unconfigured','policy_sha256':digest}
        original=source(client)
        original_digest=policy_digest({k:v for k,v in original.items()if k!='failurePolicy'})
        def desired_unchanged():
            current=source(client)
            return (policy_digest({k:v for k,v in current.items()if k!='failurePolicy'})==original_digest
                    and current.get('failurePolicy')==policy.get('failure_policy'))
        installed=manager._proxy_root_json('check-control')
        current_control=installed.get('ok')is True and installed.get('controlDigest')==manager._proxy_local_control_digest()
        if (current_control and active_config_matches(manager,policy)
                and status.get('failurePolicy')==policy.get('failure_policy')):
            proof(manager,client,deadline)
            require(binding(manager)[0]==before and desired_unchanged(),'routing_binding_changed')
            require(policy_digest(load_proxy_routing_policy())==digest,'routing_policy_changed_during_apply')
            return {'ok':True,'changed':False,'policy_sha256':digest,'exception_count':len(policy['exceptions']),'native_proxy_routing_verified':True,'native_proxy_verified':(policy.get('failure_policy')is None
                    or ((client.proxy_status().get('report')or{}).get('dataPlaneVerified')is True))}
        accepted(manager.proxy_prerequisite())
        accepted(manager.proxy_quarantine())
        require(manager._proxy_stop_agent(),'agent_stop_failed')
        controller=ProxyController(manager.context,manager.cfg,manager.lease,manager,client)
        result=controller.set_source(original['kind'],original['value'],True,selected_node=original['selectedNode'],
            udp_allowed=original['udpAllowed'],allow_insecure_http=original['allowInsecureHttp'],ipv6_allowed=original['ipv6Allowed'])
        accepted(result)
        proof(manager,client,deadline)
        require(active_config_matches(manager,policy),'routing_config_not_applied')
        require(binding(manager)[0]==before and desired_unchanged(),'routing_binding_changed')
        require(policy_digest(load_proxy_routing_policy())==digest,'routing_policy_changed_during_apply')
        return {'ok':True,'changed':True,'policy_sha256':digest,'exception_count':len(policy['exceptions']),
                'container_binding_preserved':True,'desired_proxy_preserved':True,'native_proxy_routing_verified':True,'native_proxy_verified':(policy.get('failure_policy')is None
                    or ((client.proxy_status().get('report')or{}).get('dataPlaneVerified')is True))}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    configure=sub.add_parser('configure');configure.add_argument('--json-stdin',action='store_true',required=True)
    sub.add_parser('ensure-default')
    sub.add_parser('show')
    run=sub.add_parser('reconcile');run.add_argument('--device',action='append',default=[]);run.add_argument('--all',action='store_true')
    run.add_argument('--timeout',type=int,default=180)
    args=parser.parse_args(argv)
    require(os.geteuid()==0,'routing_root_required')
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(UPSTREAM/'src'))
    from xenoid.proxy_source import load_proxy_routing_policy
    if args.action in ('configure','ensure-default','show'):
        lock=Path('/run/xenoid-qa-proxy-routing.lock').open('a');os.chmod(lock.name,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if args.action=='show':answer={'ok':True,'policy':load_proxy_routing_policy()}
            elif args.action=='ensure-default':answer=ensure_default_policy()
            else:answer=save_policy(json.load(sys.stdin))
            print(json.dumps(answer));return 0
        finally:lock.close()
    require(args.timeout>=0 and bool(args.all)!=bool(args.device),'routing_arguments_invalid')
    sys.path.insert(0,'/opt/xenoid-fleet/panel-scripts')
    import script_host as host
    host.adopt_native_environment();os.chdir(UPSTREAM)
    from xenoid.backend import RuntimeManager
    from xenoid.config import resolve_instance
    from xenoid.device_identity import RegenerationJournal
    from xenoid.convergence import ConvergenceExecutor
    from qa_resources import registry_names
    def pending(manager):
        journal=ConvergenceExecutor(manager).journal.load()
        return RegenerationJournal(manager.context).load()is not None or bool(journal and(
            journal.get('regenerationTransactionId')is not None or journal.get('bootSeedTarget')is not None))
    failed=False
    for name in registry_names()if args.all else args.device:
        try:
            require(re.fullmatch(r'phone-[1-9][0-9]*',name)is not None,'routing_arguments_invalid')
            context,config,lease=resolve_instance(name,project_root=UPSTREAM,migrate_legacy=False)
            manager=RuntimeManager(context,config,lease)
            owned,_=manager._owned_container_record(timeout=10)
            if owned is None:answer={'ok':True,'skipped':'absent'}
            else:
                broker=host.Broker(manager,lambda *_:None,config={'expected_container_id':owned['Id'],'command_timeout':args.timeout})
                answer=reconcile(manager,broker,timeout=args.timeout,pending=pending)
        except Exception as exc:
            code=getattr(exc,'code',str(exc)if isinstance(exc,RoutingError)else'routing_reconcile_failed')
            answer={'ok':False,'code':code if isinstance(code,str)and re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code)else'routing_reconcile_failed'}
        print(json.dumps({'instance':name,**answer}),flush=True);failed|=answer['ok']is not True
    return int(failed)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'ok':False,'code':getattr(exc,'code','routing_policy_invalid')}));raise SystemExit(1)
