"""Isolate Android's display-triggered VM tuning from the shared Linux host.

The stock Android extra_free_kbytes.sh adds its delta to the current global
watermark every time WM changes its property. In privileged containers this
compounds across guests and QA cycles. Replace only that known producer with a
per-guest accounting shim, before boot where possible. The host administrator
retains ownership of the host VM setting. Running guests receive a read-only
per-boot bind; stopped guests have this one file patched in their existing inner
rootfs image. The immutable Docker image and device identity are not replaced.
This module never sends input, clears app data, or changes proxies.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

STATE = Path('/var/lib/xenoid-fleet/qa-host-vm')
CONFIG = Path('/etc/xenoid-fleet/qa-host-vm.json')
WATERMARK = Path('/proc/sys/vm/watermark_scale_factor')
SCRIPT = 'system/bin/extra_free_kbytes.sh'
GUARD = b'''#!/system/bin/sh
# Xenoid QA: Android VM tuning belongs to its host, not each guest/display.
# dev.xenoid.host-vm-isolation/v1
case "$#:$1" in
    1:*) case "$1" in ''|*[!0-9]*) exit 2;; esac ;;
    *) exit 2 ;;
esac
umask 077
directory=/data/local/tmp/xenoid-host-vm
mkdir -p "$directory" || exit 1
chmod 700 "$directory" || exit 1
printf '%s\\n' "$1" > "$directory/extra_free_kbytes.requested" || exit 1
# Never write a host-global /proc/sys/vm knob from a container.
exit 0
'''
GUARD_SHA256 = hashlib.sha256(GUARD).hexdigest()
# Exact stock Android 13 script identified by the deployment source probe.
STOCK_SHA256 = '34f2a6bfe655d913d81434da1aa7bab9ff56ed5685eed791f5f033f6879f28f9'


def require(value, code):
    if not value:
        raise RuntimeError(code)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as stream:
        try:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno()); os.replace(stream.name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):os.unlink(stream.name)


@contextlib.contextmanager
def host_lock():
    STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd=os.open(STATE/'operation.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_nlink==1,
                'host_vm_lock_invalid')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:os.close(fd)


def patch_producer(root, backup, *, required_owner=0):
    """Atomic file replacement on a pinned guest root or private offline mount."""
    directory=Path(root)/'system/bin'
    target=directory/'extra_free_kbytes.sh'
    for parent in (Path(root)/'system',directory):
        parent_info=parent.lstat()
        require(stat.S_ISDIR(parent_info.st_mode)and parent_info.st_uid==required_owner
                and not parent_info.st_mode&0o022,'host_vm_script_parent_invalid')
    info=target.lstat()
    require(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and info.st_uid==required_owner
            and not info.st_mode&0o022,
            'host_vm_producer_metadata_invalid')
    fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        current=os.fstat(fd)
        require((current.st_dev,current.st_ino)==(info.st_dev,info.st_ino),'host_vm_script_file_changed')
        body=os.read(fd,16385)
    finally:os.close(fd)
    require(0<len(body)<=16384,'host_vm_script_size_invalid')
    sha=digest(body)
    if sha==GUARD_SHA256:
        require(stat.S_IMODE(info.st_mode)==0o755, 'host_vm_guard_mode_invalid')
        return {'ok':True,'changed':False,'guard_sha256':sha}
    require(sha==STOCK_SHA256, 'host_vm_unknown_producer')
    attrs={name:base64.b64encode(os.getxattr(target,name)).decode()
           for name in os.listxattr(target)}
    original={'schema':'dev.xenoid.host-vm-original/v1','sha256':sha,
              'body_base64':base64.b64encode(body).decode(),'mode':stat.S_IMODE(info.st_mode),
              'uid':info.st_uid,'gid':info.st_gid,'xattrs':attrs}
    if backup.exists():
        saved=json.loads(backup.read_text())
        require(saved['sha256']==sha and digest(base64.b64decode(saved['body_base64']))==sha,
                'host_vm_original_backup_conflict')
    else:atomic_json(backup,original)
    fd,name=tempfile.mkstemp(prefix='.xenoid-host-vm-',dir=directory)
    try:
        with os.fdopen(fd,'wb')as stream:
            stream.write(GUARD);stream.flush();os.fchmod(stream.fileno(),0o755)
            os.fchown(stream.fileno(),info.st_uid,info.st_gid)
            for key,value in attrs.items():os.setxattr(stream.fileno(),key,base64.b64decode(value))
            os.fsync(stream.fileno())
        now=target.lstat()
        require((now.st_dev,now.st_ino)==(info.st_dev,info.st_ino)and digest(target.read_bytes())==sha,
                'host_vm_producer_changed_during_patch')
        os.replace(name,target)
        directory_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
        require(digest(target.read_bytes())==GUARD_SHA256,'host_vm_guard_readback_failed')
    finally:
        with contextlib.suppress(FileNotFoundError):os.unlink(name)
    return {'ok':True,'changed':True,'original_sha256':sha,'guard_sha256':GUARD_SHA256}


def process_start(pid):
    return int((Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19])


def checked_container(manager, expected):
    owned,_=manager._owned_container_record(timeout=5)
    require(owned is not None and owned.get('Id')==expected,
            'host_vm_container_binding_changed')
    return owned


def guest_binding(owned):
    state=owned.get('State')or{}
    return (owned.get('Id'),owned.get('Image'),state.get('Running'),state.get('Pid'),state.get('StartedAt'))


def guard_running(manager, owned):
    expected=owned['Id'];pid=(owned.get('State')or{}).get('Pid')
    require(type(pid)is int and pid>1,'host_vm_guest_pid_invalid')
    start=process_start(pid);backup=STATE/'originals'/(manager.context.instance_id+'.json')
    root=Path('/proc')/str(pid)/'root';target=root/SCRIPT
    require(guest_binding(checked_container(manager,expected))==guest_binding(owned)
            and process_start(pid)==start,'host_vm_guest_changed_before_patch')
    info=target.lstat();body=target.read_bytes();sha=digest(body)
    require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_nlink==1,
            'host_vm_live_producer_metadata_invalid')
    if sha==GUARD_SHA256:
        if backup.exists():
            original=json.loads(backup.read_text())
            require(original.get('sha256')==STOCK_SHA256
                    and digest(base64.b64decode(original['body_base64']))==STOCK_SHA256,
                    'host_vm_original_backup_conflict')
            if info.st_gid!=original['gid']:
                source=root/'data/local/tmp/xenoid-host-vm/extra_free_kbytes.sh'
                existing=source.lstat()
                require(stat.S_ISREG(existing.st_mode)and existing.st_uid==0
                        and (existing.st_dev,existing.st_ino)==(info.st_dev,info.st_ino),
                        'host_vm_guard_metadata_conflict')
                os.chown(source,original['uid'],original['gid'])
        result={'ok':True,'changed':False,'guard_sha256':sha}
    else:
        require(sha==STOCK_SHA256,'host_vm_unknown_producer')
        attrs={name:base64.b64encode(os.getxattr(target,name)).decode()
               for name in os.listxattr(target)}
        original={'schema':'dev.xenoid.host-vm-original/v1','sha256':sha,
                  'body_base64':base64.b64encode(body).decode(),'mode':stat.S_IMODE(info.st_mode),
                  'uid':info.st_uid,'gid':info.st_gid,'xattrs':attrs}
        if backup.exists():
            saved=json.loads(backup.read_text())
            require(saved['sha256']==sha and digest(base64.b64decode(saved['body_base64']))==sha,
                    'host_vm_original_backup_conflict')
        else:atomic_json(backup,original)
        directory=root/'data/local/tmp/xenoid-host-vm'
        directory.mkdir(mode=0o700,exist_ok=True)
        directory_info=directory.lstat()
        require(stat.S_ISDIR(directory_info.st_mode)and directory_info.st_uid==0
                and stat.S_IMODE(directory_info.st_mode)==0o700,'host_vm_guest_directory_invalid')
        source=directory/'extra_free_kbytes.sh'
        if source.exists():
            source_info=source.lstat()
            require(stat.S_ISREG(source_info.st_mode)and source_info.st_uid==0
                    and source_info.st_nlink==1 and digest(source.read_bytes())==GUARD_SHA256,
                    'host_vm_guard_source_conflict')
            if (source_info.st_uid,source_info.st_gid)!=(info.st_uid,info.st_gid):
                os.chown(source,info.st_uid,info.st_gid)
        else:
            fd,name=tempfile.mkstemp(prefix='.producer-',dir=directory)
            try:
                with os.fdopen(fd,'wb')as stream:
                    stream.write(GUARD);stream.flush();os.fchmod(stream.fileno(),0o755)
                    os.fchown(stream.fileno(),info.st_uid,info.st_gid)
                    for key,value in attrs.items():os.setxattr(stream.fileno(),key,base64.b64decode(value))
                    os.fsync(stream.fileno())
                os.replace(name,source)
            finally:
                with contextlib.suppress(FileNotFoundError):os.unlink(name)
        require(process_start(pid)==start and guest_binding(checked_container(manager,expected))==guest_binding(owned),
                'host_vm_guest_changed_before_mount')
        namespace=(Path('/proc')/str(pid)/'ns/mnt').stat()
        payload={'pid':pid,'start_ticks':start,'namespace_device':namespace.st_dev,
                 'namespace_inode':namespace.st_ino}
        mounted=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--bind-child'],
                               input=json.dumps(payload).encode(),capture_output=True,timeout=15)
        if mounted.returncode!=0:
            codes=re.findall(r'host_vm_[a-z0-9_]+',mounted.stderr.decode(errors='replace'))
            raise RuntimeError(codes[-1]if codes else'host_vm_init_namespace_bind_failed')
        after=target.stat();source_info=source.stat()
        require((after.st_dev,after.st_ino)==(source_info.st_dev,source_info.st_ino)
                and digest(target.read_bytes())==GUARD_SHA256,'host_vm_bind_readback_failed')
        entries=[line.split()for line in (Path('/proc')/str(pid)/'mountinfo').read_text().splitlines()]
        require(any(line[4]=='/'+SCRIPT and 'ro'in line[5].split(',')for line in entries),
                'host_vm_bind_not_readonly')
        result={'ok':True,'changed':True,'original_sha256':sha,'guard_sha256':GUARD_SHA256}
    require(guest_binding(checked_container(manager,expected))==guest_binding(owned)
            and process_start(pid)==start,'host_vm_guest_changed_after_patch')
    return {**result,'method':'init_namespace_readonly_bind_or_persistent_guard',
            'container_id':expected,'instance_id':manager.context.instance_id,'pid':pid,'start_ticks':start}


def bind_child(payload):
    """Preload host libc, enter PID1's mount/root views, then issue mount syscalls."""
    import ctypes
    pid=payload['pid'];require(type(pid)is int and pid>1,'host_vm_bind_pid_invalid')
    require(process_start(pid)==payload['start_ticks'],'host_vm_bind_process_changed')
    root=Path('/proc')/str(pid)/'root'
    source=root/'data/local/tmp/xenoid-host-vm/extra_free_kbytes.sh';target=root/SCRIPT
    info=source.lstat()
    require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_nlink==1
            and stat.S_IMODE(info.st_mode)==0o755 and digest(source.read_bytes())==GUARD_SHA256,
            'host_vm_bind_source_invalid')
    require(digest(target.read_bytes())in (STOCK_SHA256,GUARD_SHA256),'host_vm_bind_target_changed')
    fd=os.open(Path('/proc')/str(pid)/'ns/mnt',os.O_RDONLY|os.O_CLOEXEC)
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    stat_fd=os.open(Path('/proc')/str(pid)/'stat',os.O_RDONLY|os.O_CLOEXEC)
    try:
        ns=os.fstat(fd)
        require((ns.st_dev,ns.st_ino)==(payload['namespace_device'],payload['namespace_inode'])
                and process_start(pid)==payload['start_ticks'],'host_vm_bind_namespace_changed')
        libc=ctypes.CDLL(None,use_errno=True)
        libc.mount.argtypes=(ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_char_p)
        libc.mount.restype=ctypes.c_int
        if libc.setns(fd,0x00020000)!=0:raise OSError(ctypes.get_errno(),'host_vm_setns_failed')
        # A path on the observer's old root is outside the joined mount
        # namespace. Change fs root through the preopened, pinned guest root;
        # keep the host proc/stat fd to fence the host PID without guest /proc.
        os.fchdir(root_fd);os.chroot('.');os.chdir('/')
        source=Path('/data/local/tmp/xenoid-host-vm/extra_free_kbytes.sh');target=Path('/'+SCRIPT)
        now=source.stat()
        require((now.st_dev,now.st_ino)==(info.st_dev,info.st_ino)
                and digest(source.read_bytes())==GUARD_SHA256,'host_vm_bind_root_changed')
        if libc.mount(os.fsencode(source),os.fsencode(target),None,4096,None)!=0:
            raise OSError(ctypes.get_errno(),'host_vm_bind_failed')
        if libc.mount(None,os.fsencode(target),None,4096|32|1|2|4,None)!=0:
            raise OSError(ctypes.get_errno(),'host_vm_bind_readonly_failed')
        fields=os.pread(stat_fd,16384,0).decode().rsplit(')',1)[1].split()
        require(int(fields[19])==payload['start_ticks'],'host_vm_bind_process_changed')
    finally:os.close(fd);os.close(root_fd);os.close(stat_fd)
    return {'ok':True,'bind_readonly':True,'pid':pid}


def active_image_loops(image):
    """Inspect loop backing inode/device too; a renamed/deleted path still counts."""
    image=Path(image);info=image.stat();wanted=str(image.resolve())
    command=['losetup','--json','--output','NAME,BACK-FILE,BACK-INO,BACK-MAJ:MIN']
    probe=subprocess.run(command,capture_output=True,text=True,timeout=8)
    require(probe.returncode==0,'host_vm_loop_inventory_unavailable')
    rows=json.loads(probe.stdout).get('loopdevices')or[]
    require(isinstance(rows,list),'host_vm_loop_inventory_invalid')
    found=[];device=f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
    for row in rows:
        name=str(row.get('back-file')or'').removesuffix(' (deleted)')
        same_path=bool(name and str(Path('/'+name.lstrip('/')).resolve())==wanted)
        same_inode=(str(row.get('back-ino'))==str(info.st_ino)
                    and row.get('back-maj:min')==device)
        if same_path or same_inode:found.append(row.get('name'))
    return found


def stopped_container_probe(payload):
    result=subprocess.run([*payload['docker_base'],'inspect','--type','container',payload['container_id']],
                          capture_output=True,text=True,timeout=8)
    require(result.returncode==0,'host_vm_offline_container_unavailable')
    values=json.loads(result.stdout);require(isinstance(values,list)and len(values)==1,'host_vm_offline_container_invalid')
    own=values[0];state=own.get('State')or{}
    require(own.get('Id')==payload['container_id']and own.get('Image')==payload['image_id']
            and state.get('Running')is False and state.get('Pid')==0
            and all((own.get('Config',{}).get('Labels')or{}).get(k)==v for k,v in payload['owner_labels'].items()),
            'host_vm_offline_guest_changed')


def image_uuid(image):
    result=subprocess.run(['blkid','-p','-s','UUID','-o','value','--',str(image)],
                          capture_output=True,text=True,timeout=8)
    require(result.returncode==0,'host_vm_rootfs_uuid_unavailable')
    return result.stdout.strip().lower()


def offline_image(manager, owned):
    require((owned.get('State')or{}).get('Running')is False,'host_vm_offline_guest_running')
    attached,status=manager._storage_growth_container()
    require(status.get('ok')is True and attached and attached.get('Id')==owned['Id'],
            'host_vm_offline_attachment_conflict')
    volume,_=manager._inspect_docker_object('volume',manager.lease.volume_name)
    require(volume and manager._volume_matches_lease(volume),'host_vm_volume_binding_invalid')
    from xenoid.storage import StorageStateStore
    storage=StorageStateStore(manager.context,manager.lease).load()
    require(storage and storage.get('state')=='committed'
            and storage.get('rootfsImage')=='xenoid-rootfs.img'
            and isinstance(storage.get('rootfsFilesystemUuid'),str)
            and type(storage.get('observedRootfsSizeBytes'))is int,
            'host_vm_committed_rootfs_required')
    image=Path(volume['Mountpoint'])/'xenoid-rootfs.img';info=image.lstat()
    require(stat.S_ISREG(info.st_mode)and info.st_nlink==1 and info.st_uid==0
            and info.st_size==storage['observedRootfsSizeBytes']>0,'host_vm_rootfs_metadata_invalid')
    actual=manager._inspect_volume_image(volume,image_name='xenoid-rootfs.img')
    require(actual.get('ok')is True and actual.get('filesystemUuid')==storage['rootfsFilesystemUuid']
            and actual.get('logicalSizeBytes')==info.st_size,'host_vm_rootfs_identity_mismatch')
    require(not active_image_loops(image),'host_vm_rootfs_still_mounted')
    require(guest_binding(checked_container(manager,owned['Id']))==guest_binding(owned),
            'host_vm_guest_changed_before_offline_patch')
    return image,info,storage['rootfsFilesystemUuid']


def guard_stopped(manager, owned, *, restore=False):
    image,info,uuid=offline_image(manager,owned)
    payload={'image':str(image),'device':info.st_dev,'inode':info.st_ino,
             'size':info.st_size,'instance_id':manager.context.instance_id,'rootfs_uuid':uuid,
             'container_id':owned['Id'],'image_id':owned['Image'],
             'docker_base':manager.docker_base_cmd(),'owner_labels':manager.lease.owner_labels,
             'restore_original':restore}
    result=subprocess.run(['unshare','--mount','--propagation','private','--',
                           sys.executable,str(Path(__file__).resolve()),'--offline-child'],
                          input=json.dumps(payload).encode(),capture_output=True,timeout=60,env=manager.docker_env())
    require(result.returncode==0,'host_vm_offline_patch_failed')
    answer=json.loads(result.stdout)
    require(guest_binding(checked_container(manager,owned['Id']))==guest_binding(owned),
            'host_vm_guest_changed_after_offline_patch')
    after=image.stat()
    require((after.st_dev,after.st_ino,after.st_size)==(info.st_dev,info.st_ino,info.st_size)
            and image_uuid(image)==uuid,'host_vm_rootfs_binding_changed')
    return {**answer,'method':'offline_restore_original'if restore else'offline_ext4_before_first_boot','container_id':owned['Id'],
            'instance_id':manager.context.instance_id,'rootfs_uuid':uuid,'image_bytes':info.st_size}


def mountpoint_present(name):
    # Temporary mountpoint names are generated without spaces or escapes.
    return any(line.split()[4]==name for line in Path('/proc/self/mountinfo').read_text().splitlines())


def offline_child(payload):
    require(re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',payload['instance_id'])is not None,
            'host_vm_instance_id_invalid')
    image=Path(payload['image']);info=image.lstat()
    require(stat.S_ISREG(info.st_mode)and info.st_uid==0 and info.st_nlink==1
            and (info.st_dev,info.st_ino,info.st_size)==(payload['device'],payload['inode'],payload['size'])
            and image.name=='xenoid-rootfs.img' and image_uuid(image)==payload['rootfs_uuid']
            and not active_image_loops(image),'host_vm_offline_image_changed')
    stopped_container_probe(payload)
    name=tempfile.mkdtemp(prefix='xenoid-host-vm-root-')
    try:
        subprocess.run(['mount','-t','ext4','-o','loop,nosuid,nodev',str(image),name],check=True,
                       capture_output=True,timeout=12)
        require(mountpoint_present(name),'host_vm_offline_mount_not_visible')
        stopped_container_probe(payload)
        require(image_uuid(image)==payload['rootfs_uuid'],'host_vm_offline_uuid_changed')
        backup=STATE/'originals'/(payload['instance_id']+'.json')
        result=(restore_producer(Path(name),backup)if payload.get('restore_original')is True
                else patch_producer(Path(name),backup))
    finally:
        # NEVER recursively clean a possibly mounted guest filesystem. A failed
        # or timed-out umount leaves the empty host directory for later cleanup;
        # process exit discards this private mount namespace safely.
        if mountpoint_present(name):
            subprocess.run(['umount',name],check=True,capture_output=True,timeout=12)
        if not mountpoint_present(name):os.rmdir(name)
    require(image_uuid(image)==payload['rootfs_uuid'],'host_vm_offline_uuid_changed')
    return result


def ensure_guest_guard(manager, expected_container_id=None):
    require(os.geteuid()==0,'host_vm_root_required')
    if CONFIG.exists()and json.loads(CONFIG.read_text()).get('enabled')is False:
        return {'ok':True,'skipped':'host_vm_isolation_disabled'}
    with host_lock():
        if CONFIG.exists()and json.loads(CONFIG.read_text()).get('enabled')is False:
            return {'ok':True,'skipped':'host_vm_isolation_disabled'}
        owned,_=manager._owned_container_record(timeout=5)
        require(owned is not None,'host_vm_owned_container_missing')
        require(expected_container_id is None or owned['Id']==expected_container_id,
                'host_vm_container_binding_changed')
        running=(owned.get('State')or{}).get('Running')is True
        result=guard_running(manager,owned)if running else guard_stopped(manager,owned)
        receipt=STATE/'guards'/(manager.context.instance_id+'.json')
        previous=json.loads(receipt.read_text())if receipt.exists()else{}
        if (result.get('changed')is True or not running or not previous
                or previous.get('container_id')!=owned['Id']):
            from datetime import datetime,timezone
            stamp=datetime.now(timezone.utc).isoformat()
            document={**result,'recorded_at':stamp,'container_running_at_patch':running,
                      'boot_phase':'running_init_namespace'if running else'before_android_init'}
            atomic_json(receipt,document)
            atomic_json(STATE/'guard-history'/(manager.context.instance_id+'-'+str(__import__('time').time_ns())+'.json'),document)
        return result


def configured_watermark(paths=None):
    """Use a configured host value; do not replay arbitrary sysctl files."""
    if CONFIG.exists():
        profile=json.loads(CONFIG.read_text());value=profile.get('watermark_scale_factor')
        if value is not None:
            require(type(value)is int and 1<=value<=3000,'host_vm_config_invalid')
            return value,'qa-host-vm.json'
    if paths is None:
        selected={}
        # Procps precedence: later directories override same basenames, while
        # lexicographically later filenames apply last; sysctl.conf is final.
        for directory in ('/lib/sysctl.d','/usr/lib/sysctl.d','/usr/local/lib/sysctl.d','/run/sysctl.d','/etc/sysctl.d'):
            for p in Path(directory).glob('*.conf'):selected[p.name]=p
        paths=[selected[k]for k in sorted(selected)]+[Path('/etc/sysctl.conf')]
    found=None
    for path in paths:
        if not path.exists():continue
        for line in path.read_text().splitlines():
            line=re.split(r'[;#]',line,maxsplit=1)[0].strip().lstrip('-')
            match=re.fullmatch(r'vm[./]watermark_scale_factor\s*=\s*(\d+)\s*',line)
            if match:
                value=int(match[1]);require(1<=value<=3000,'host_vm_sysctl_config_invalid')
                found=value,str(path)
    return found or (10,'Linux kernel default; no administrator override found')


def restore_host_watermark(managers):
    """Explicit migration only, after every running registered producer is guarded."""
    require(os.geteuid()==0,'host_vm_root_required')
    require_registered_managers(managers)
    require(not CONFIG.exists()or json.loads(CONFIG.read_text()).get('enabled')is not False,
            'host_vm_host_restore_requires_enabled_isolation')
    with host_lock():
        verified=[]
        for manager in managers:
            owned,_=manager._owned_container_record(timeout=5)
            if owned is None or (owned.get('State')or{}).get('Running')is not True:continue
            pid=owned['State']['Pid'];start=process_start(pid)
            require(digest((Path('/proc')/str(pid)/'root'/SCRIPT).read_bytes())==GUARD_SHA256,
                    'host_vm_unguarded_running_guest')
            require(guest_binding(checked_container(manager,owned['Id']))==guest_binding(owned)
                    and process_start(pid)==start,'host_vm_guest_changed_during_verification')
            verified.append({'instance_id':manager.context.instance_id,'container_id':owned['Id']})
        require(bool(verified),'host_vm_no_guarded_running_guests')
        before=WATERMARK.read_text().strip();value,source=configured_watermark()
        original=STATE/'original-host-watermark.json'
        if not original.exists():atomic_json(original,{'value':int(before),'note':'Pre-fix value; may already be contaminated by Android. Not the recommended default.'})
        if before!=str(value):WATERMARK.write_text(str(value)+'\n')
        require(WATERMARK.read_text().strip()==str(value),'host_vm_host_readback_failed')
        result={'ok':True,'before':int(before),'after':value,'source':source,
                'guarded_running_guests':verified,'continuous_reset':False}
        atomic_json(STATE/'last-explicit-host-restore.json',result)
        return result


def require_registered_managers(managers):
    from qa_resources import registry_names
    expected=set(registry_names())
    observed=[manager.context.instance_name for manager in managers]
    require(len(observed)==len(set(observed))and set(observed)==expected,
            'host_vm_all_registered_instances_required')


def restore_producer(root, backup, *, required_owner=0):
    """Restore an original only on an offline, already validated inner rootfs."""
    target=Path(root)/SCRIPT;info=target.lstat();current=target.read_bytes()
    require(stat.S_ISREG(info.st_mode)and info.st_uid==required_owner and info.st_nlink==1,
            'host_vm_restore_metadata_invalid')
    if digest(current)==STOCK_SHA256:
        return {'ok':True,'changed':False,'restored_sha256':STOCK_SHA256}
    require(digest(current)==GUARD_SHA256,'host_vm_restore_unknown_producer')
    require(backup.is_file(),'host_vm_restore_backup_missing')
    saved=json.loads(backup.read_text());original=base64.b64decode(saved['body_base64'])
    require(saved.get('sha256')==STOCK_SHA256 and digest(original)==STOCK_SHA256
            and saved.get('uid')==required_owner,'host_vm_restore_backup_invalid')
    fd,name=tempfile.mkstemp(prefix='.xenoid-host-vm-restore-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb')as stream:
            stream.write(original);stream.flush();os.fchmod(stream.fileno(),saved['mode'])
            os.fchown(stream.fileno(),saved['uid'],saved['gid'])
            for key,value in saved['xattrs'].items():os.setxattr(stream.fileno(),key,base64.b64decode(value))
            os.fsync(stream.fileno())
        now=target.lstat()
        require((now.st_dev,now.st_ino)==(info.st_dev,info.st_ino)
                and digest(target.read_bytes())==GUARD_SHA256,'host_vm_restore_target_changed')
        os.replace(name,target)
        descriptor=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        require(digest(target.read_bytes())==STOCK_SHA256,'host_vm_restore_readback_failed')
    finally:
        with contextlib.suppress(FileNotFoundError):os.unlink(name)
    return {'ok':True,'changed':True,'restored_sha256':STOCK_SHA256}


def rollback_producers(managers, *, host_value=None):
    """Administrator rollback; never stop/recreate a device implicitly."""
    require(os.geteuid()==0,'host_vm_root_required')
    require(host_value is None or type(host_value)is int and 1<=host_value<=3000,
            'host_vm_rollback_host_value_invalid')
    require_registered_managers(managers)
    def stopped(manager):
        owned,_=manager._owned_container_record(timeout=5)
        require(owned is not None and (owned.get('State')or{}).get('Running')is False,
                'host_vm_rollback_requires_stopped_containers')
        number=manager.context.instance_name.removeprefix('phone-')
        require(re.fullmatch(r'[1-9][0-9]*',number)is not None,'host_vm_instance_name_invalid')
        unit=subprocess.run(['systemctl','show','xenoid-panel-script@'+number+'.service',
                             '--property=LoadState,MainPID,ActiveState'],capture_output=True,text=True,timeout=5)
        values=dict(line.split('=',1)for line in unit.stdout.splitlines()if '='in line)
        require((unit.returncode==0 or values.get('LoadState')=='not-found')and values.get('ActiveState')=='inactive'
                and values.get('MainPID')=='0','host_vm_rollback_requires_paused_workers')
        return owned
    managers=sorted(managers,key=lambda manager:manager.context.instance_name)
    # Native always takes an instance lock before the host guard lock. Acquire
    # all affected instance locks in a stable order, and retain them through
    # disabling, offline restores, final checks and the optional host write.
    with contextlib.ExitStack()as locks:
        for manager in managers:locks.enter_context(manager.instance_operation_lock(timeout_seconds=180))
        with host_lock():
            require_registered_managers(managers)
            for manager in managers:stopped(manager)
            previous=json.loads(CONFIG.read_text())if CONFIG.exists()else{}
            if not (STATE/'original-guard-config.json').exists():atomic_json(STATE/'original-guard-config.json',previous)
            atomic_json(CONFIG,{**previous,'enabled':False})
            results=[]
            for manager in managers:
                result=guard_stopped(manager,stopped(manager),restore=True)
                require(result.get('ok')is True and result.get('restored_sha256')==STOCK_SHA256,
                        'host_vm_rollback_original_not_verified')
                results.append(result)
            require_registered_managers(managers)
            for manager in managers:stopped(manager)
            desired,source=configured_watermark()if host_value is None else(host_value,'explicit administrator rollback value')
            before=int(WATERMARK.read_text())
            if before!=desired:WATERMARK.write_text(str(desired)+'\n')
            require(int(WATERMARK.read_text())==desired,'host_vm_rollback_host_readback_failed')
            answer={'ok':True,'isolation_enabled':False,'restored_producers':results,
                    'host_before':before,'host_after':desired,'host_value_source':source,
                    'devices_started':False,'bad_captured_factor_restored_automatically':False}
            atomic_json(STATE/'last-producer-rollback.json',answer)
            return answer


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--all',action='store_true')
    p.add_argument('--restore-host',action='store_true')
    p.add_argument('--rollback-producers',action='store_true')
    p.add_argument('--host-value',type=int)
    p.add_argument('--offline-child',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--bind-child',action='store_true',help=argparse.SUPPRESS)
    args=p.parse_args();require(os.geteuid()==0,'host_vm_root_required')
    if args.offline_child:
        print(json.dumps(offline_child(json.load(sys.stdin))));return
    if args.bind_child:
        print(json.dumps(bind_child(json.load(sys.stdin))));return
    require(args.all,'host_vm_all_required')
    sys.path.insert(0,'/opt/xenoid-fleet/panel-scripts')
    from script_host import adopt_native_environment
    adopt_native_environment()
    from qa_resources import registry_names,native_manager
    managers=[native_manager(name)for name in registry_names()]
    if args.rollback_producers:
        require(not args.restore_host,'host_vm_conflicting_operations')
        print(json.dumps(rollback_producers(managers,host_value=args.host_value)),flush=True);return
    require(args.host_value is None,'host_vm_host_value_only_for_rollback')
    results=[]
    for manager in managers:
        with manager.instance_operation_lock(timeout_seconds=180):
            result=ensure_guest_guard(manager)
            results.append(result);print(json.dumps(result),flush=True)
    if args.restore_host:print(json.dumps({'host_restore':restore_host_watermark(managers)}),flush=True)


if __name__=='__main__':main()
