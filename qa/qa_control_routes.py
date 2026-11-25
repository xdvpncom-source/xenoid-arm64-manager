"""Keep only the owned native ADB TCP reply on its physical Android underlay.

Call under the existing native instance operation lock. No ADB, daemon request,
VPN mutation, public destination exception, IPv6 write or input action is used.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

STATE = Path('/var/lib/xenoid-fleet/control-routes')
OWNER = 0
PROC = Path('/proc')
PRIORITY = 900
SCHEMA = 'dev.xenoid.native-control-route/v1'


def require(value, code):
    if not value:
        raise RuntimeError('control_route_' + code)


def start_ticks(path):
    return int(path.read_text().rsplit(')', 1)[1].split()[19])


def private_read(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(before.st_mode) and before.st_uid == OWNER
            and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
            and 0 < before.st_size <= 16384, 'unsafe_record')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns), 'record_changed')
        body = stream.read(16385)
        after = os.fstat(stream.fileno())
    require(len(body) <= 16384 and all(getattr(opened, k) == getattr(after, k) for k in
            ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_uid', 'st_mode', 'st_nlink')), 'record_changed')
    return json.loads(body)


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (STATE.parent, STATE, path.parent):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
                'unsafe_directory')
    fd, temporary = tempfile.mkstemp(prefix='.route-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def socket_uid(guest_proc, network_proc, port):
    listeners = {}
    for name in ('tcp', 'tcp6'):
        for line in (network_proc / name).read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 10 and fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) == port:
                listeners[int(fields[9])] = int(fields[7])
    require(listeners and len(set(listeners.values())) == 1, 'native_listener_missing')
    uid = next(iter(listeners.values()))
    # Native adbd runs as root or Android's shell service, never an app UID.
    require(uid in (0, 2000), 'native_socket_uid_invalid')
    owners = []
    for process in guest_proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            generation = start_ticks(process / 'stat')
            if (process / 'comm').read_text().strip() != 'adbd':
                continue
            executable = os.readlink(process / 'exe')
            require(executable == '/system/bin/adbd' or re.fullmatch(
                    r'/apex/com\.android\.adbd(?:@\d+)?/bin/adbd', executable), 'native_executable_invalid')
            values = next(line.split()[1:] for line in (process / 'status').read_text().splitlines()
                          if line.startswith('Uid:'))
            require(values == [str(uid)] * 4, 'native_process_uid_mismatch')
            sockets = set()
            for entry in (process / 'fd').iterdir():
                try:
                    target = os.readlink(entry)
                except FileNotFoundError:
                    continue  # An unrelated accepted client FD may close.
                match = re.fullmatch(r'socket:\[(\d+)\]', target)
                if match:
                    sockets.add(int(match[1]))
            after_uid = next(line.split()[1:] for line in (process / 'status').read_text().splitlines()
                             if line.startswith('Uid:'))
            require(start_ticks(process / 'stat') == generation and after_uid == values, 'native_process_changed')
            if sockets & listeners.keys():
                owners.append(process.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(len(owners) == 1, 'native_socket_owner_unproven')
    return uid


def normalized_rule(row):
    allowed = {'priority', 'src', 'dst', 'iif', 'ipproto', 'sport', 'uid_start', 'uid_end', 'table', 'protocol'}
    if set(row) - allowed or row.get('protocol') not in (None, 'boot'):
        return None
    try:
        port = row['sport']
        if isinstance(port, dict):
            require(set(port) == {'start', 'end'} and port['start'] == port['end'], 'port_range')
            port = port['start']
        return {'priority': int(row['priority']), 'src': str(ipaddress.ip_network(row['src'], strict=False)),
                'dst': str(ipaddress.ip_network(row['dst'], strict=False)), 'iif': row['iif'],
                'ipproto': str(row['ipproto']), 'sport': int(port), 'uid_start': int(row['uid_start']),
                'uid_end': int(row['uid_end']), 'table': int(row['table'])}
    except (KeyError, TypeError, ValueError, RuntimeError):
        return None


class Guest:
    def __init__(self, manager, expected, deadline):
        self.manager, self.expected, self.deadline = manager, expected, deadline
        self.fd = None

    def __enter__(self):
        require(re.fullmatch('[0-9a-f]{64}', self.expected), 'invalid_container_id')
        self.owned, _ = self.manager._owned_container_record(timeout=self.timeout())
        require(self.owned and self.owned.get('Id') == self.expected
                and self.owned.get('State', {}).get('Running') is True, 'owned_runtime_unavailable')
        state = self.owned['State']; self.pid = state['Pid']
        require(type(self.pid) is int and self.pid > 1, 'invalid_pid')
        self.process = PROC / str(self.pid)
        self.start = start_ticks(self.process / 'stat')
        require((self.process / 'uid_map').read_text().split() == ['0', '0', '4294967295'], 'uid_map_unproven')
        self.fd = os.open(self.process / 'ns/net', os.O_RDONLY | os.O_CLOEXEC)
        try:
            self.ns = os.fstat(self.fd)
            self.binding = {'container_id': self.expected, 'image': self.owned['Image'], 'pid': self.pid,
                            'started_at': state['StartedAt'], 'start_ticks': self.start,
                            'netns_device': self.ns.st_dev, 'netns_inode': self.ns.st_ino,
                            'instance_id': self.manager.context.instance_id, 'resource_tag': self.manager.context.resource_tag}
            lease = self.manager.lease
            network, _ = self.manager._inspect_docker_object('network', lease.network_name, timeout=self.timeout())
            require(network and self.manager._network_matches_lease(network), 'network_lease_mismatch')
            endpoints = self.owned.get('NetworkSettings', {}).get('Networks', {})
            require(len(endpoints) == 1, 'network_endpoint_mismatch')
            endpoint = next(iter(endpoints.values()))
            require((endpoint.get('NetworkID'), endpoint.get('IPAddress'), endpoint.get('Gateway'),
                     endpoint.get('GlobalIPv6Address'), endpoint.get('IPv6Gateway'), endpoint.get('MacAddress')) ==
                    (network['Id'], lease.ipv4_address, lease.ipv4_gateway, lease.ipv6_address,
                     lease.ipv6_gateway, lease.mac_address.lower()), 'network_endpoint_mismatch')
            for version in (4, 6):
                address = ipaddress.ip_address(getattr(lease, f'ipv{version}_address'))
                gateway = ipaddress.ip_address(getattr(lease, f'ipv{version}_gateway'))
                subnet = ipaddress.ip_network(getattr(lease, f'ipv{version}_subnet'))
                require(address.version == gateway.version == version and address.is_private and gateway.is_private
                        and address in subnet and gateway in subnet and address != gateway, 'nonprivate_lease')
            links = self.ip('-j', 'address', 'show', 'dev', 'rmnet_data0', 'scope', 'global')
            require(len(links) == 1, 'physical_interface_missing')
            link = links[0]; index = link.get('ifindex')
            require(link.get('ifname') == 'rmnet_data0' and type(index) is int and 0 < index < (1 << 31)
                    and link.get('address') == lease.mac_address.lower(), 'physical_interface_mismatch')
            require(sum(item.get('family') == 'inet' and item.get('local') == lease.ipv4_address
                        and item.get('prefixlen') == ipaddress.ip_network(lease.ipv4_subnet).prefixlen
                        for item in link.get('addr_info', [])) == 1, 'physical_address_mismatch')
            self.table = 1000 + index
            routes = self.ip('-4', '-j', 'route', 'show', 'table', str(self.table))
            require(any(r.get('dst') == lease.ipv4_subnet and r.get('dev') == 'rmnet_data0'
                        and r.get('scope') == 'link' for r in routes)
                    and any(r.get('dst') == 'default' and r.get('gateway') == lease.ipv4_gateway
                            and r.get('dev') == 'rmnet_data0' for r in routes), 'physical_routes_unproven')
            # /proc/net is a self/net symlink. Use the held target process's
            # network view rather than resolving host-self in the guest procfs.
            uid = socket_uid(self.process / 'root/proc', self.process / 'net', lease.android_adb_port)
            self.rule = {'priority': PRIORITY, 'src': lease.ipv4_address + '/32', 'dst': lease.ipv4_gateway + '/32',
                         'iif': 'lo', 'ipproto': 'tcp', 'sport': lease.android_adb_port,
                         'uid_start': uid, 'uid_end': uid, 'table': self.table}
            self.check()
            return self
        except BaseException:
            os.close(self.fd); self.fd = None
            raise

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)

    def timeout(self):
        value = 10 if self.deadline is None else min(10, self.deadline - time.monotonic())
        require(value > 0, 'deadline')
        return value

    def check(self):
        current, _ = self.manager._owned_container_record(timeout=self.timeout())
        require(current and all(current.get(k) == self.owned.get(k) for k in ('Id', 'Image'))
                and all(current.get('State', {}).get(k) == self.owned['State'].get(k)
                        for k in ('Running', 'Pid', 'StartedAt'))
                and start_ticks(self.process / 'stat') == self.start
                and ((self.process / 'ns/net').stat().st_dev, (self.process / 'ns/net').stat().st_ino) ==
                    (self.ns.st_dev, self.ns.st_ino), 'binding_changed')

    def ip(self, *arguments):
        self.check()
        result = subprocess.run([shutil.which('nsenter') or '/usr/bin/nsenter',
                                 '--net=/proc/self/fd/' + str(self.fd), '--',
                                 shutil.which('ip') or '/usr/sbin/ip', *arguments],
                                pass_fds=(self.fd,), capture_output=True, timeout=self.timeout(), check=False)
        self.check()
        require(result.returncode == 0 and len(result.stdout) <= 262144, 'netlink_failed')
        return json.loads(result.stdout) if '-j' in arguments else None

    def rules(self):
        rows = self.ip('-4', '-j', 'rule', 'show')
        require(any(r.get('priority') == 0 and r.get('table') == 'local' for r in rows), 'local_rule_missing')
        require(not any(0 < r.get('priority', 0) < PRIORITY for r in rows), 'earlier_rule_conflict')
        return [r for r in rows if r.get('priority') == PRIORITY]

    def change(self, action):
        r = self.rule
        self.ip('-4', 'rule', action, 'pref', str(PRIORITY), 'from', r['src'], 'to', r['dst'],
                'iif', 'lo', 'ipproto', 'tcp', 'sport', str(r['sport']),
                'uidrange', str(r['uid_start']) + '-' + str(r['uid_end']), 'lookup', str(self.table))


def transaction(manager, expected_container_id, *, deadline=None, restore=False):
    require(os.geteuid() == 0, 'root_required')
    require(re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                         manager.context.instance_id), 'invalid_instance_id')
    for directory in (STATE.parent, STATE, STATE / manager.context.instance_id):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022, 'unsafe_directory')
    with Guest(manager, expected_container_id, deadline) as guest:
        path = STATE / manager.context.instance_id / (expected_container_id + '-' + str(guest.start) + '.json')
        old = private_read(path)
        rows = guest.rules()
        require(not rows or len(rows) == 1 and normalized_rule(rows[0]) == guest.rule, 'foreign_priority')
        if old is not None:
            require(old.get('schema') == SCHEMA and old.get('binding') == guest.binding
                    and old.get('rule') == guest.rule and old.get('phase') in ('pending', 'committed', 'restored', 'rollback_pending'),
                    'record_binding_mismatch')
        if restore:
            require(old is not None, 'restore_record_missing')
            if rows:
                guest.change('del')
            require(not guest.rules(), 'restore_readback_failed')
            save(path, {**old, 'phase': 'restored'})
            return {'ok': True, 'restored': True, 'ipv6_changed': False}
        require(old is None or old['phase'] in ('committed', 'restored'), 'unresolved_intent')
        require(not rows or old and old['phase'] == 'committed', 'unowned_identical_rule')
        if rows:
            return {'ok': True, 'changed': False, 'ipv6_changed': False}
        record = {'schema': SCHEMA, 'binding': guest.binding, 'rule': guest.rule, 'phase': 'pending'}
        save(path, record)  # Durable ownership intent precedes the only write.
        try:
            guest.change('add')
            require([normalized_rule(r) for r in guest.rules()] == [guest.rule], 'add_readback_failed')
            save(path, {**record, 'phase': 'committed'})
            return {'ok': True, 'changed': True, 'ipv6_changed': False}
        except BaseException:
            # A timeout may occur after netlink accepted the rule. Re-read the
            # exact tuple, never flush or delete an unknown priority occupant.
            try:
                rows = guest.rules()
                require(not rows or [normalized_rule(r) for r in rows] == [guest.rule], 'rollback_foreign_rule')
                if rows:
                    guest.change('del')
                require(not guest.rules(), 'rollback_readback_failed')
                save(path, {**record, 'phase': 'restored'})
            except BaseException:
                save(path, {**record, 'phase': 'rollback_pending'})
            raise


def ensure_control_routes(manager, expected_container_id, *, deadline=None):
    return transaction(manager, expected_container_id, deadline=deadline)


def restore_control_routes(manager, expected_container_id, *, deadline=None):
    return transaction(manager, expected_container_id, deadline=deadline, restore=True)
