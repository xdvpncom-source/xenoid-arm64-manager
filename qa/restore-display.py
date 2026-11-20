#!/usr/bin/env python3
"""Review or restore the display settings on one managed Xenoid host.

Run on the managed host as root. The default only prints a plan:
  python3 xenoid-restore-display.py --device 1
  python3 xenoid-restore-display.py --all

After stopping the shared script in the management panel:
  python3 xenoid-restore-display.py --device 1 --apply
  python3 xenoid-restore-display.py --all --apply

This restores display/FPS changes only. It does not remove compressed swap,
change APKs, clear application data, or replace a container/data volume.
When applying, it disables this host's resource defaults and backs up their
configuration so a later container boot does not reapply the low display.
The installed /opt/xenoid-fleet/panel-scripts helper is required.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/opt/xenoid-fleet/panel-scripts')
try:
    import script_host as host
except ImportError as error:
    raise SystemExit('The managed host panel-scripts helper is required.') from error
sys.path.insert(0, str(host.UPSTREAM / 'src'))
os.chdir(host.UPSTREAM)
from xenoid.config import resolve_instance
from xenoid.backend import RuntimeManager


RESOURCE_CONFIG = Path('/etc/xenoid-fleet/qa-resources.json')
RESOURCE_STATE = Path('/var/lib/xenoid-fleet/qa-resources')


@contextmanager
def resource_apply_lock(number, apply):
    # Take the addon lock before the native operation lock, matching the timer.
    # Existing legacy hosts without an addon require no new configuration/state.
    if not apply or not (RESOURCE_CONFIG.exists() or RESOURCE_STATE.exists()):
        yield
        return
    RESOURCE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(RESOURCE_STATE / ('phone-' + str(number) + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def disable_resource_defaults(backup):
    """Opt this host out before rollback; keep all other administrator settings."""
    if not RESOURCE_CONFIG.exists():
        return {'changed':False, 'status':'not-installed'}
    if RESOURCE_CONFIG.is_symlink():
        raise RuntimeError('The resource configuration must be a regular host file')
    original = RESOURCE_CONFIG.read_bytes()
    config = json.loads(original)
    if not isinstance(config, dict):
        raise RuntimeError('The resource configuration must be a JSON object')
    if config.get('enabled') is False:
        return {'changed':False, 'status':'already-disabled'}
    saved = backup / 'qa-resources-before.json'
    saved.write_bytes(original)
    saved.chmod(0o600)
    config['enabled'] = False
    with tempfile.NamedTemporaryFile(mode='w', dir=RESOURCE_CONFIG.parent, delete=False) as stream:
        try:
            json.dump(config, stream, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
            os.replace(stream.name, RESOURCE_CONFIG)
        finally:
            if os.path.exists(stream.name):
                os.unlink(stream.name)
    return {'changed':True, 'status':'disabled', 'original_config_backup':str(saved)}


def effective_metrics(size, density):
    sizes = re.findall(r'(?:Physical|Override) size:\s*(\d+)x(\d+)', size)
    densities = re.findall(r'(?:Physical|Override) density:\s*(\d+)', density)
    if not sizes or not densities:
        raise RuntimeError('The saved display dimensions or density are invalid')
    width, height = map(int, sizes[-1])
    dpi = int(densities[-1])
    if min(width, height, dpi) <= 0:
        raise RuntimeError('Saved display values must be positive')
    return {'width': width, 'height': height, 'density': dpi}


def find_original(number, execute, backup_root=None):
    search = [Path(backup_root)] if backup_root else [
        Path('/var/backups/xenoid-reliability-fix-20261120'),
        Path('/var/backups/xenoid-low-display-fleet-20261120'),
        Path('/var/backups/xenoid-low-display-20261120')]
    saved_metrics = None
    saved_rate = None
    rate_known = False
    evidence = []
    for root in search:
        early = root / 'display-before.json'
        if saved_metrics is None and early.is_file():
            document = json.loads(early.read_text())
            for row in document:
                if row.get('device_id') == number:
                    saved_metrics = effective_metrics(row['size_before'], row['density_before'])
                    evidence.append(str(early))
                    break
        fleet = root / ('phone-' + str(number) + '.json')
        if fleet.is_file():
            before = json.loads(fleet.read_text())
            if saved_metrics is None:
                saved_metrics = effective_metrics(before['size'], before['density'])
                evidence.append(str(fleet))
            if not rate_known:
                text = before.get('display', {}).get('config', '')
                saved_rate = (str(text).strip() + '\n').encode() if str(text).strip() else None
                rate_known = True
        manual = root / ('phone-' + str(number)) / 'metadata.json'
        if manual.is_file() and not rate_known:
            before = json.loads(manual.read_text())
            value = manual.parent / 'refresh.before'
            if before.get('config_existed'):
                if not value.is_file():
                    raise RuntimeError('Missing original refresh file: ' + str(value))
                saved_rate = value.read_bytes()
            rate_known = True
            evidence.append(str(manual))
    addon_original = RESOURCE_STATE / ('phone-' + str(number) + '-original-display.json')
    if saved_metrics is None and addon_original.is_file():
        original = json.loads(addon_original.read_text())
        width, height = original['size']
        dpi = original['density']
        if any(type(value) is not int or value <= 0 for value in (width, height, dpi)):
            raise RuntimeError('The original addon display backup is invalid')
        saved_metrics = {'width':width, 'height':height, 'density':dpi}
        evidence.append(str(addon_original))
    if saved_metrics is None:
        result = execute(['cat', '/data/local/tmp/xenoid-profile/effective.json'])
        display = json.loads(result.stdout).get('display', {})
        values = [int(display.get(key, 0)) for key in ('width', 'height', 'densityDpi')]
        if min(values) <= 0:
            raise RuntimeError('No original backup or valid native display profile was found')
        saved_metrics = dict(zip(('width', 'height', 'density'), values))
        evidence.append('native effective.json display profile (no earlier metrics backup)')
    if not rate_known:
        # This dedicated file did not exist before this display patch. If no
        # saved rate exists, restoring the original vendor library ignores it.
        evidence.append('original refresh backup absent; remove the QA-specific rate file')
    return saved_metrics, saved_rate, evidence


def restore(number, options):
    context, settings, lease = resolve_instance('phone-' + str(number),
                                               project_root=host.UPSTREAM, migrate_legacy=False)
    manager = RuntimeManager(context, settings, lease)
    owned, reason = manager._owned_container_record(timeout=8)
    if not owned:
        raise RuntimeError(reason or 'The managed container is unavailable')
    if not owned.get('State', {}).get('Running'):
        return {'device_id':number, 'status':'stopped', 'changed':False}
    broker = host.Broker(manager, lambda *args:None,
                         config={'command_timeout':options.timeout,
                                 'expected_container_id':owned['Id']})
    deadline = time.monotonic() + options.timeout if options.timeout else None
    with broker.owned_manager() as checked:
        def execute(args, **kwargs):
            return broker.native_display_exec(checked, args, deadline=deadline, **kwargs)

        current = broker.display_state(execute)
        size = execute(['wm','size']).stdout.decode()
        density = execute(['wm','density']).stdout.decode()
        current_metrics = effective_metrics(size, density)
        metrics, rate, evidence = find_original(number, execute, options.backup_root)
        read = execute(['sh','-c', f'if [ -f {host.DISPLAY_RATE_FILE} ]; then cat {host.DISPLAY_RATE_FILE}; else exit 42; fi'], check=False)
        if read.returncode not in (0,42):
            raise RuntimeError('Unable to read the current QA refresh file')
        current_rate = read.stdout if read.returncode == 0 else None
        our_mount = current.get('sha') == host.DISPLAY_LIBRARY_SHA256 and current.get('mounts',0) > 0
        active = host.snapshot(host.STATE / str(number), number)
        active_script = active.get('state') not in host.TERMINAL | {'missing'}
        plan = {'device_id':number, 'status':'plan', 'apply':options.apply,
                'container_id':owned['Id'], 'backup_sources':evidence,
                'current_metrics':current_metrics, 'restore_metrics':metrics,
                'unmount_our_library':our_mount,
                'current_library_sha256':current.get('sha'),
                'restore_rate_file':rate.decode(errors='replace') if rate is not None else None,
                'active_script':active_script,
                'disable_host_resource_defaults':RESOURCE_CONFIG.exists()}
        if not options.apply:
            return plan
        if active_script:
            raise RuntimeError('Stop this device shared script in the management panel before applying rollback')
        backup = Path('/var/backups') / ('xenoid-display-rollback-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        resource_defaults = disable_resource_defaults(backup)
        snapshot = backup / ('phone-' + str(number) + '.json')
        snapshot.write_text(json.dumps({'container_id':owned['Id'], 'display':current,
                                       'size':size, 'density':density,
                                       'rate_file_base64':base64.b64encode(current_rate).decode() if current_rate is not None else None,
                                       'plan':plan}, indent=2))
        snapshot.chmod(0o600)
        graphics_changed = False
        network_result = None
        if our_mount:
            # Remove only a layer containing the exact bundled QA library.
            # Stop as soon as an underlying original/administrator layer appears.
            while current.get('sha') == host.DISPLAY_LIBRARY_SHA256 and current.get('mounts',0)>0:
                previous_count = current['mounts']
                removed = execute(['umount', host.DISPLAY_LIBRARY], check=False)
                if removed.returncode:
                    detail = removed.stderr.decode(errors='replace').strip()
                    if 'busy' not in detail.casefold():
                        raise RuntimeError(detail or 'The QA display mount could not be removed')
                    # A running composer can retain a reference to its mapped
                    # library. Detach only this verified QA mount from the
                    # namespace, then restart graphics below to release the old
                    # mapping and load the original read-only vendor library.
                    execute(['umount','-l',host.DISPLAY_LIBRARY])
                current = broker.display_state(execute)
                if current['mounts'] >= previous_count:
                    raise RuntimeError('The QA display mount could not be removed')
                graphics_changed = True
        if current_rate != rate:
            if rate is None:
                execute(['rm','-f',host.DISPLAY_RATE_FILE])
            else:
                execute(['sh','-c', f'set -e; f=$(mktemp {host.DISPLAY_RATE_FILE}.XXXXXX); '
                         'trap \'rm -f "$f"\' EXIT; cat >"$f"; chmod 0644 "$f"; '
                         f'mv -f "$f" {host.DISPLAY_RATE_FILE}'], data=rate)
            graphics_changed = True
        if graphics_changed:
            previous_pid = execute(['pidof','surfaceflinger'], check=False).stdout.decode().strip()
            execute(['setprop','ctl.restart','vendor.hwcomposer-2-1'])
            broker.display_wait(execute, 0, deadline, previous_pid=previous_pid)
            broker.display_bootstrap(checked, execute, deadline)
            reconcile_proxy = getattr(broker, 'display_proxy_reconcile', None)
            if reconcile_proxy is not None:
                network_result = reconcile_proxy(checked, deadline, force=True, stopping=False)
            else:
                network_result = {'ok':False, 'ready':False,
                                  'code':'helper_upgrade_required_for_proxy_recovery'}
        # Explicit original values are required: after composer restart its
        # physical mode is 720x1280 even when the original profile used 1440x3120.
        execute(['wm','size',str(metrics['width'])+'x'+str(metrics['height'])])
        execute(['wm','density',str(metrics['density'])])
        verified = effective_metrics(execute(['wm','size']).stdout.decode(),
                                     execute(['wm','density']).stdout.decode())
        if verified != metrics:
            raise RuntimeError('Original display dimensions/density could not be verified')
        after = broker.display_state(execute)
        if our_mount and after.get('sha') == host.DISPLAY_LIBRARY_SHA256:
            raise RuntimeError('The QA composer library is still mounted')
        network_pending = network_result is not None and network_result.get('ready') is not True
        return {**plan, 'status':'display-restored-network-pending' if network_pending else 'restored',
                'graphics_restarted':graphics_changed, 'network_recovery':network_result,
                'verified_metrics':verified, 'verified_display':after,
                'resource_defaults':resource_defaults,
                'pre_rollback_backup':str(snapshot)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument('--device', type=int, action='append', help='Repeat for multiple device numbers')
    targets.add_argument('--all', action='store_true', help='All registered phone-N instances on this host')
    parser.add_argument('--apply', action='store_true', help='Apply the printed restoration plan; default is dry-run')
    parser.add_argument('--timeout', type=int, default=180, help='Per-device timeout in seconds; 0 is unlimited')
    parser.add_argument('--backup-root', help='Override the default original-backup directory search')
    options = parser.parse_args()
    if options.timeout < 0:
        parser.error('--timeout must be non-negative')
    if os.geteuid() != 0:
        parser.error('Run as root on the managed host so registry and original backups can be read')
    numbers = sorted({int(path.name[6:]) for path in (host.UPSTREAM/'.xenoid/instances').glob('phone-*')
                      if path.is_dir() and path.name[6:].isdigit()}) if options.all else sorted(set(options.device))
    if not numbers or any(number <= 0 for number in numbers):
        parser.error('Select at least one valid positive registered device number')
    failures = 0
    for number in numbers:
        try:
            with resource_apply_lock(number, options.apply):
                result = restore(number, options)
            print(json.dumps(result), flush=True)
            if result.get('status') == 'display-restored-network-pending':
                failures += 1
        except Exception as error:
            failures += 1
            print(json.dumps({'device_id':number, 'status':'failed', 'error':str(error)}), flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
