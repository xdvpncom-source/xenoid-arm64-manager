#!/usr/bin/env python3
"""Preserve usr-merge while retaining Xenoid's exact Ubuntu amd64 loader link."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
import time

LOADER='ld-linux-x86-64.so.2'
TARGET='/usr/x86_64-linux-gnu/lib/'+LOADER
SCHEMA='dev.xenoid.fleet.lib64-layout/v1'

def require(ok,code):
    if not ok:raise RuntimeError(code)

def identity(p):
    s=p.lstat();return {'device':s.st_dev,'inode':s.st_ino,'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid}

def safe_dir(p,owner=0):
    s=p.lstat();require(stat.S_ISDIR(s.st_mode)and not p.is_symlink()and s.st_uid==owner and s.st_gid==(0 if owner==0 else os.getgid()) and stat.S_IMODE(s.st_mode)==0o755,'unsafe_directory:'+str(p))

def no_builds(proc=Path('/proc')):
    active=[]
    for p in proc.iterdir():
        if not p.name.isdigit()or int(p.name)==os.getpid():continue
        try:exe=os.readlink(p/'exe')
        except OSError:continue
        if any(x in Path(exe).name for x in ('qemu-x86_64','x86_64-binfmt')):active.append(int(p.name))
    require(not active,'active_amd64_emulation')

def unowned_loader():
    r=subprocess.run(['dpkg-query','-S','/lib64/'+LOADER],capture_output=True,text=True)
    require(r.returncode==1 and not r.stdout.strip(),'loader_path_owned_by_package_or_query_failed')

def inspect_layout(root=Path('/'),owner=0,check_system=True):
    root=Path(root);base=root/'lib64';usr=root/'usr';target=root/TARGET.lstrip('/')
    for name in ('bin','sbin','lib'):
        p=root/name;require(p.is_symlink()and os.readlink(p)in('usr/'+name,'/usr/'+name),'other_unmerged_root_path:'+name)
    for name in ('lib32','libo32','libx32'):
        p=root/name
        if p.exists()or p.is_symlink():require(p.is_symlink()and os.readlink(p).lstrip('/').startswith('usr/lib'),'other_unmerged_arch_path:'+name)
    safe_dir(usr,owner)
    result={'schema':SCHEMA,'root':str(root),'read_only':True,'loader_target':TARGET}
    if base.is_symlink():
        link=os.readlink(base);require(link.lstrip('/').startswith('usr/lib')and '..'not in Path(link).parts,'lib64_link_not_usr_merged')
        require((root/link.lstrip('/')).is_dir(),'lib64_target_missing')
        result.update(action='already_merged',lib64=identity(base),link=link);return result
    if not base.exists():
        result.update(action='create_merged_lib64');return result
    safe_dir(base,owner);require(sorted(x.name for x in base.iterdir())==[LOADER],'lib64_has_unrecognized_files')
    leaf=base/LOADER;require(leaf.is_symlink()and os.readlink(leaf)==TARGET and leaf.lstat().st_uid==owner,'unrecognized_loader_link')
    data=target.read_bytes();meta=target.lstat();require(stat.S_ISREG(meta.st_mode)and meta.st_uid==owner and not meta.st_mode&0o022,'unsafe_cross_loader')
    require(data[:6]==b'\x7fELF\x02\x01'and int.from_bytes(data[18:20],'little')==62,'cross_loader_not_amd64')
    dst=root/'usr/lib64'
    if dst.exists()or dst.is_symlink():safe_dir(dst,owner);require(not list(dst.iterdir()),'usr_lib64_not_empty')
    require(base.stat().st_dev==usr.stat().st_dev,'usr_on_different_filesystem')
    if check_system:no_builds();unowned_loader()
    result.update(action='repair_owned_legacy_lib64',lib64=identity(base),leaf=identity(leaf),loader_sha256=hashlib.sha256(data).hexdigest(),usr_lib64_existed=dst.exists())
    return result

def exchange(a,b):
    libc=ctypes.CDLL(None,use_errno=True);fn=libc.renameat2;fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];fn.restype=ctypes.c_int
    if fn(-100,os.fsencode(a),-100,os.fsencode(b),2):raise OSError(ctypes.get_errno(),'atomic_layout_exchange_failed')

def syncdir(p):
    fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def apply_layout(root=Path('/'),owner=0,check_system=True):
    root=Path(root);plan=inspect_layout(root,owner,check_system)
    if plan['action']=='already_merged':return {**plan,'ok':True,'changed':False}
    if check_system:no_builds()
    require(inspect_layout(root,owner,check_system)==plan,'layout_changed_before_preparation')
    dst=root/'usr/lib64';base=root/'lib64'
    if not dst.exists():dst.mkdir(mode=0o755)
    safe_dir(dst,owner)
    if plan['action']=='create_merged_lib64':
        require(not base.exists()and not base.is_symlink(),'lib64_appeared')
        base.symlink_to('usr/lib64');syncdir(root)
        return {**plan,'ok':True,'read_only':False,'changed':True,'link':'usr/lib64'}
    backups=root/'var/lib/xenoid-loader-layout';backups.mkdir(mode=0o700,parents=True,exist_ok=True)
    st=backups.lstat();require(stat.S_ISDIR(st.st_mode)and st.st_uid==owner and not st.st_mode&0o077 and st.st_dev==base.stat().st_dev,'unsafe_backup_directory')
    directory=Path(tempfile.mkdtemp(prefix='lib64-',dir=backups));replacement=directory/'lib64-before';replacement.symlink_to('usr/lib64')
    linked=dst/LOADER;require(not linked.exists()and not linked.is_symlink(),'destination_loader_appeared');linked.symlink_to(TARGET)
    exchanged=False
    try:
        require(identity(base)==plan['lib64']and identity(base/LOADER)==plan['leaf']
                and sorted(x.name for x in base.iterdir())==[LOADER],'layout_changed_before_exchange')
        exchange(base,replacement);exchanged=True;syncdir(root);syncdir(dst)
        require(base.is_symlink()and os.readlink(base)=='usr/lib64','merged_link_readback_failed')
        require(identity(replacement)==plan['lib64'],'original_directory_changed')
        require(hashlib.sha256((root/TARGET.lstrip('/')).read_bytes()).hexdigest()==plan['loader_sha256'],'loader_target_changed')
        require(os.readlink(base)=='usr/lib64'and os.readlink(linked)==TARGET,'loader_path_changed')
        if root==Path('/'):require((base/LOADER).resolve()==(root/TARGET.lstrip('/')).resolve(),'resolved_loader_path_changed')
        receipt={**plan,'ok':True,'read_only':False,'changed':True,'at':time.time(),'backup':str(directory),'replacement':identity(linked),'root_link':identity(base),'packages_modified':False}
        (directory/'receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');(directory/'receipt.json').chmod(0o600);syncdir(directory)
        return receipt
    except BaseException:
        if exchanged and base.is_symlink()and os.readlink(base)=='usr/lib64'and identity(replacement)==plan['lib64']:exchange(base,replacement);syncdir(root)
        if linked.is_symlink()and os.readlink(linked)==TARGET:linked.unlink()
        if not plan['usr_lib64_existed']and not list(dst.iterdir()):dst.rmdir()
        raise

def rollback_layout(receipt_path):
    receipt_path=Path(receipt_path)
    require(receipt_path.is_absolute()and receipt_path.name=='receipt.json'
            and receipt_path.parent.parent==Path('/var/lib/xenoid-loader-layout')
            and not receipt_path.is_symlink(),'rollback_path_invalid')
    s=receipt_path.lstat();require(s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o600,'rollback_receipt_not_private')
    r=json.loads(receipt_path.read_text());require(r.get('schema')==SCHEMA and r.get('root')=='/'
            and r.get('action')=='repair_owned_legacy_lib64'and r.get('backup')==str(receipt_path.parent),'rollback_receipt_invalid')
    no_builds();base=Path('/lib64');old=receipt_path.parent/'lib64-before';leaf=Path('/usr/lib64')/LOADER
    require(base.is_symlink()and os.readlink(base)=='usr/lib64'and identity(base)==r['root_link']
            and identity(old)==r['lib64']and identity(leaf)==r['replacement'],'rollback_layout_changed')
    require(sorted(p.name for p in Path('/usr/lib64').iterdir())==[LOADER]
            and os.readlink(leaf)==TARGET,'rollback_destination_changed')
    require(hashlib.sha256(Path(TARGET).read_bytes()).hexdigest()==r['loader_sha256'],'rollback_loader_changed')
    exchange(base,old);syncdir(Path('/'));leaf.unlink()
    if not r['usr_lib64_existed']:Path('/usr/lib64').rmdir()
    require(identity(base)==r['lib64']and os.readlink(base/LOADER)==TARGET,'rollback_readback_failed')
    return {'ok':True,'rolled_back':True,'receipt':str(receipt_path),'packages_modified':False}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',action='store_true');p.add_argument('--rollback',type=Path);a=p.parse_args()
    require(os.geteuid()==0 and platform.system()=='Linux'and platform.machine()in('aarch64','arm64'),'native_arm64_root_required')
    if not a.execute:
        require(a.rollback is None,'rollback_requires_execute');print(json.dumps(inspect_layout(),sort_keys=True));return
    fd=os.open('/run/xenoid-lib64-layout.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd);require(st.st_uid==0 and stat.S_ISREG(st.st_mode)and st.st_nlink==1 and not st.st_mode&0o077,'unsafe_layout_lock');fcntl.flock(fd,fcntl.LOCK_EX)
        print(json.dumps(rollback_layout(a.rollback)if a.rollback else apply_layout(),sort_keys=True))
    finally:os.close(fd)
if __name__=='__main__':main()
