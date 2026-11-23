#!/usr/bin/env python3
"""Metadata-preserving boot-ID write for an explicitly selected cold profile.

The caller is the existing native boot identity owner under the instance lock.
No identifier is generated here. A mounted data image is used only so the one
file can be replaced atomically with its existing metadata intact.
"""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
LABEL = b'u:object_r:shell_data_file:s0\0'

class BootSeedError(Exception): pass

def require(ok, code):
    if not ok: raise BootSeedError(code)

def command(args, timeout=30):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    require(result.returncode == 0,'boot_seed_tool_failed')
    return result.stdout

def read_attrs(fd):
    return {name:os.getxattr(fd,name) for name in os.listxattr(fd)}

def write_attrs(fd, attrs):
    for name,value in attrs.items():os.setxattr(fd,name,value)

def read_leaf(fd):
    os.lseek(fd,0,os.SEEK_SET)
    value=os.read(fd,65)
    require(len(value)<=64 and UUID.fullmatch(value.decode('ascii').removesuffix('\n')) is not None,'boot_seed_value_invalid')
    return value

def replace_boot_id(directory, boot_id, *, owner_uid=0):
    require(isinstance(boot_id,str) and UUID.fullmatch(boot_id) is not None,'boot_seed_value_invalid')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    dirfd=os.open(directory,flags)
    oldfd=newfd=None;temporary=None
    try:
        info=os.fstat(dirfd)
        require(info.st_uid==owner_uid and not info.st_mode&0o022,'boot_seed_directory_unsafe')
        oldfd=os.open('boot_id',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=dirfd)
        before=os.fstat(oldfd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid==owner_uid and before.st_nlink==1,'boot_seed_file_unsafe')
        attrs=read_attrs(oldfd)
        require(attrs.get('security.selinux')==LABEL,'boot_seed_label_mismatch')
        require(not any(k in attrs for k in ('security.ima','security.evm','security.capability')),'boot_seed_metadata_unsupported')
        old=read_leaf(oldfd);desired=(boot_id+'\n').encode('ascii')
        if old==desired:return {'ok':True,'already':True,'metadataPreserved':True}
        # An O_EXCL staging inode remains private until value, owner, mode and
        # every original xattr have been verified and synced.
        import secrets
        temporary='.boot-id-'+secrets.token_hex(12)
        newfd=os.open(temporary,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=dirfd)
        os.fchown(newfd,before.st_uid,before.st_gid)
        os.fchmod(newfd,stat.S_IMODE(before.st_mode))
        write_attrs(newfd,attrs)
        require(os.write(newfd,desired)==len(desired),'boot_seed_write_failed')
        os.fsync(newfd)
        staged=os.fstat(newfd)
        require((staged.st_uid,staged.st_gid,stat.S_IMODE(staged.st_mode))==(before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode)) and read_attrs(newfd)==attrs and read_leaf(newfd)==desired,'boot_seed_metadata_readback_failed')
        current=os.stat('boot_id',dir_fd=dirfd,follow_symlinks=False)
        require((current.st_dev,current.st_ino)==(before.st_dev,before.st_ino) and read_leaf(oldfd)==old,'boot_seed_target_changed')
        os.replace(temporary,'boot_id',src_dir_fd=dirfd,dst_dir_fd=dirfd);temporary=None
        os.fsync(dirfd)
        check=os.open('boot_id',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=dirfd)
        try:
            final=os.fstat(check)
            require((final.st_uid,final.st_gid,stat.S_IMODE(final.st_mode))==(before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode)) and read_attrs(check)==attrs and read_leaf(check)==desired,'boot_seed_metadata_readback_failed')
        finally:os.close(check)
        return {'ok':True,'already':False,'metadataPreserved':True}
    finally:
        if oldfd is not None:os.close(oldfd)
        if newfd is not None:os.close(newfd)
        if temporary is not None:os.unlink(temporary,dir_fd=dirfd)
        os.close(dirfd)

def main():
    require(os.geteuid()==0,'root_required')
    request=json.load(sys.stdin)
    require(isinstance(request,dict) and set(request)=={'volume','labels','dataUuid','rootfsUuid','bootId'},'boot_seed_request_invalid')
    require(all(isinstance(request.get(k),str) and UUID.fullmatch(request[k]) is not None for k in ('dataUuid','rootfsUuid','bootId')),'boot_seed_request_invalid')
    labels=request['labels'];volume=request['volume']
    require(isinstance(labels,dict) and set(labels)=={'dev.xenoid.owner','dev.xenoid.schema','dev.xenoid.instance_id','dev.xenoid.instance_name','dev.xenoid.resource_tag'},'boot_seed_owner_invalid')
    instance=labels['dev.xenoid.instance_id']
    require(isinstance(instance,str) and UUID.fullmatch(instance) is not None,'boot_seed_owner_invalid')
    tag=hashlib.sha256(b'xenoid-instance/v1\0'+instance.encode('ascii')).hexdigest()[:12]
    require(labels['dev.xenoid.owner']=='xenoid' and labels['dev.xenoid.schema']=='1' and labels['dev.xenoid.resource_tag']==tag and volume=='xenoid-data-'+tag,'boot_seed_owner_invalid')
    record=json.loads(command(['docker','volume','inspect',volume]))
    require(len(record)==1 and all(record[0].get('Labels',{}).get(k)==v for k,v in labels.items()),'boot_seed_owner_invalid')
    require(not command(['docker','ps','-q','--filter','volume='+volume]).strip(),'volume_in_use')
    base=Path(record[0]['Mountpoint']);data=base/'xenoid-data.img'
    for path,expected in ((data,request['dataUuid']),(base/'xenoid-rootfs.img',request['rootfsUuid'])):
        for parent in (path,*path.parents):require(not parent.is_symlink(),'unsafe_image_path')
        info=path.stat();require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and not info.st_mode&0o022,'unsafe_image_file')
        require(command(['blkid','-p','-s','UUID','-o','value',str(path)]).decode().strip().lower()==expected,'storage_uuid_mismatch')
        require(not command(['losetup','-j',str(path)]).strip(),'image_still_attached')
    fsck=subprocess.run(['e2fsck','-pf',str(data)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=600)
    require(fsck.returncode in (0,1),'boot_seed_fsck_failed')
    require(not command(['docker','ps','-q','--filter','volume='+volume]).strip() and not command(['losetup','-j',str(data)]).strip(),'volume_in_use')
    mountpoint=Path(tempfile.mkdtemp(prefix='xenoid-boot-seed-'))
    mounted=False
    def interrupted(signum,frame):raise BootSeedError('boot_seed_interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:
        command(['mount','-o','loop,nosuid,nodev,noexec,noatime',str(data),str(mountpoint)])
        mounted=True
        mounted_identity=command(['findmnt','-n','-o','UUID,FSTYPE','--mountpoint',str(mountpoint)]).decode().split()
        require(mounted_identity==[request['dataUuid'],'ext4'],'boot_seed_mount_identity_mismatch')
        directory=mountpoint
        for part in ('local','tmp','xenoid-profile'):
            directory=directory/part
            info=directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),'boot_seed_directory_unsafe')
        result=replace_boot_id(directory,request['bootId'])
    finally:
        if mounted:
            command(['umount',str(mountpoint)])
            mounted=False
        mountpoint.rmdir()
    require(not command(['losetup','-j',str(data)]).strip(),'boot_seed_unmount_ambiguous')
    print(json.dumps(result,separators=(',',':')))

if __name__=='__main__':
    try:main()
    except (BootSeedError,ValueError,TypeError,KeyError,OSError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error) if isinstance(error,BootSeedError) else 'boot_seed_failed'}))
        sys.exit(1)
