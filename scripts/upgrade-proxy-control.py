#!/usr/bin/env python3
"""Refresh enabled running proxies once per verified native control/process revision."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

UPSTREAM = Path('/opt/xenoid-fleet/upstream')
STATE = Path('/var/lib/xenoid-fleet/proxy-control')
SERVER_ROOT = Path('/run/xenoid/proxy')


class UpgradeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise UpgradeError(code)


def accepted(result):
    require(isinstance(result, dict) and result.get('ok') is True,
            result.get('code', result.get('error', 'proxy_upgrade_pending')) if isinstance(result, dict)
            else 'proxy_upgrade_pending')


def binding(manager):
    owned, _ = manager._owned_container_record(timeout=10)
    require(owned is not None, 'native_runtime_unavailable')
    return {'instance_id': manager.context.instance_id, 'resource_tag': manager.context.resource_tag,
            'container_id': owned['Id'], 'data': [row for row in owned.get('Mounts', [])
                                                 if row.get('Destination') == '/data']}, owned


def desired(client):
    status, exported = client.proxy_status(), client.proxy_export()
    accepted(status)
    accepted(exported)
    require(isinstance(exported.get('source'), dict), 'proxy_source_unreadable')
    policy = {key: value for key, value in status.items() if key in {
        'enabled', 'udpAllowed', 'ipv6Allowed', 'allowInsecureHttp', 'sourceKind'} or 'selected' in key.lower()}
    return hashlib.sha256(json.dumps({'source': exported['source'], 'policy': policy}, sort_keys=True).encode()).hexdigest()


def process(manager):
    record = json.loads((SERVER_ROOT / manager.context.resource_tag / 'engine-server.json').read_text())
    require(type(record.get('pid')) is int and record['pid'] > 1 and type(record.get('starttime')) is int,
            'proxy_process_identity_unavailable')
    return record


def strict_proxy(manager, client, deadline):
    from xenoid.live_observe import LiveAcceptance, _Budget, _Checks
    status, bootstrap = client.proxy_status(), client.bootstrap_status()
    checks = _Checks()
    LiveAcceptance(manager)._proxy_checks(manager, {}, {'proxyGeneration': status.get('generation'),
        'runtimeEpoch': bootstrap.get('runtimeEpoch')}, {}, {'proxy': status, 'bootstrap': bootstrap},
        _Budget(deadline), checks)
    require(checks.first_failure() is None, checks.first_failure() or 'proxy_upgrade_pending')


def refresh(manager, broker, *, timeout, pending, proof=strict_proxy):
    deadline = time.monotonic() + timeout if timeout else None
    with manager.instance_operation_lock(timeout_seconds=timeout or None), broker.display_raw_docker(manager, deadline):
        before, owned = binding(manager)
        if (owned.get('State') or {}).get('Running') is not True:
            return {'ok': True, 'skipped': 'stopped'}
        client = manager.daemon_client()
        status = client.proxy_status()
        accepted(status)
        require(status.get('stateReadable') is True and type(status.get('configured')) is bool
                and type(status.get('enabled')) is bool, 'proxy_state_unreadable')
        if status.get('configured') is not True or status.get('enabled') is not True:
            return {'ok': True, 'skipped': 'disabled_or_unconfigured'}
        require(not pending(manager), 'native_regeneration_pending')
        original_source = desired(client)
        source_sha = hashlib.sha256((UPSTREAM / 'scripts/xenoid-proxy-engine.py').read_bytes()).hexdigest()
        control = manager._proxy_local_control_digest()
        marker = STATE / (manager.context.instance_id + '.json')
        try:
            previous = json.loads(marker.read_text()) if marker.exists() else {}
        except (OSError, ValueError):
            previous = {}
        try:
            installed = manager._proxy_root_json('check-control')
            disk_current = (installed.get('ok') is True and installed.get('controlDigest') == control
                            and manager._proxy_remote_sha256(manager._proxy_installed_helper) == source_sha)
            current = {'binding': before, 'engine_sha256': source_sha, 'control_digest': control}
            if disk_current and all(previous.get(key) == value for key, value in current.items()):
                try:
                    same_process = previous.get('process') == process(manager)
                except (OSError, ValueError, UpgradeError):
                    same_process = False
                if same_process:
                    proof(manager, client, deadline)
                    require(binding(manager)[0] == before and desired(client) == original_source, 'proxy_upgrade_binding_changed')
                    return {'ok': True, 'skipped': 'already_current'}
            accepted(manager.proxy_prerequisite())
            accepted(manager.proxy_quarantine())
            require(manager._proxy_stop_agent(), 'agent_stop_failed')
            accepted(manager.reconcile_proxy_desired())
            proof(manager, client, deadline)
            require(binding(manager)[0] == before and desired(client) == original_source, 'proxy_upgrade_binding_changed')
            installed = manager._proxy_root_json('check-control')
            require(installed.get('ok') is True and installed.get('controlDigest') == control
                    and manager._proxy_remote_sha256(manager._proxy_installed_helper) == source_sha, 'install_artifact_invalid')
            current['process'] = process(manager)
            STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
            temporary = marker.with_suffix('.tmp')
            with temporary.open('w') as stream:
                os.chmod(temporary, 0o600)
                json.dump(current, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            return {'ok': True, 'changed': True, 'container_id_preserved': True, 'desired_proxy_preserved': True}
        except Exception:
            # A failed proof invalidates earlier attestation, permitting one
            # fresh native bootstrap on an explicit retry of this command.
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            # Never quarantine a replacement container after losing the binding.
            try:
                if binding(manager)[0] == before:
                    manager.proxy_quarantine()
            except Exception:
                pass
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', action='append', default=[])
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args(argv)
    require(os.geteuid() == 0 and args.timeout >= 0, 'proxy_upgrade_arguments_invalid')
    sys.path.insert(0, '/opt/xenoid-fleet/panel-scripts')
    import script_host as host
    host.adopt_native_environment()
    sys.path.insert(0, str(UPSTREAM / 'src'))
    os.chdir(UPSTREAM)
    from xenoid.backend import RuntimeManager
    from xenoid.config import resolve_instance
    from xenoid.device_identity import RegenerationJournal
    from xenoid.convergence import ConvergenceExecutor
    from qa_resources import registry_names
    def pending(manager):
        journal = ConvergenceExecutor(manager).journal.load()
        return RegenerationJournal(manager.context).load() is not None or bool(journal and (
            journal.get('regenerationTransactionId') is not None or journal.get('bootSeedTarget') is not None))
    STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    host_lock = (STATE / '.upgrade.lock').open('a')
    fcntl.flock(host_lock, fcntl.LOCK_EX)
    failed = False
    for name in registry_names() if args.all else args.device:
        try:
            require(re.fullmatch(r'phone-[1-9][0-9]*', name) is not None, 'proxy_upgrade_arguments_invalid')
            context, config, lease = resolve_instance(name, project_root=UPSTREAM, migrate_legacy=False)
            manager = RuntimeManager(context, config, lease)
            owned, _ = manager._owned_container_record(timeout=10)
            if owned is None:
                result = {'ok': True, 'skipped': 'absent'}
            else:
                broker = host.Broker(manager, lambda *_: None, config={'expected_container_id': owned['Id'],
                    'instance_id': context.instance_id, 'resource_tag': context.resource_tag, 'command_timeout': args.timeout})
                result = refresh(manager, broker, timeout=args.timeout, pending=pending)
        except Exception as error:
            reason = getattr(error, 'code', None)
            result = {'ok': False, 'code': 'proxy_control_upgrade_failed',
                      'reason': reason if isinstance(reason, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason)
                      else type(error).__name__}
        print(json.dumps({'instance': name, **result}), flush=True)
        failed |= result['ok'] is not True
    host_lock.close()
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
