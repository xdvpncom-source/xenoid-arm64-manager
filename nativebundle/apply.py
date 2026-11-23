#!/usr/bin/env python3
"""Apply one explicitly pinned public native source bundle; no runtime actions."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

SCHEMA='dev.xenoid.fleet.native-source-bundle/v1'
BASE='c07e56b4aef8cd7e4d56eed11c84781369d29997'
GENERATED='native/xenoid-proxy-sandbox/xenoid-proxy-sandbox'


def require(ok,code):
    if not ok:raise RuntimeError('native_bundle_'+code)


def sha(data):return hashlib.sha256(data).hexdigest()


def regular(path):
    meta=path.lstat()
    require(stat.S_ISREG(meta.st_mode) and meta.st_uid==os.geteuid()
            and not meta.st_mode&0o022,'untrusted_file:'+str(path))
    with path.open('rb')as stream:
        opened=os.fstat(stream.fileno());data=stream.read();after=os.fstat(stream.fileno())
    require((meta.st_dev,meta.st_ino)==(opened.st_dev,opened.st_ino)
            and (opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)==
            (after.st_size,after.st_mtime_ns,after.st_ctime_ns),'file_changed')
    return data,stat.S_IMODE(meta.st_mode)


def safe_parent(root,relative):
    path=Path(relative)
    require(not path.is_absolute() and path.parts and all(p not in ('.','..') for p in path.parts),
            'invalid_relative_path')
    current=root
    for part in path.parts[:-1]:
        current=current/part
        if current.exists() or current.is_symlink():
            meta=current.lstat()
            require(stat.S_ISDIR(meta.st_mode) and not current.is_symlink()
                    and meta.st_uid==os.geteuid() and not meta.st_mode&0o022,'unsafe_parent')
    return root/path


def load_bundle(bundle,pin):
    raw,_=regular(bundle/'manifest.json');require(sha(raw)==pin,'manifest_pin_changed')
    manifest=json.loads(raw)
    require(manifest.get('schema')==SCHEMA and manifest.get('upstream_revision')==BASE,'manifest_schema')
    rows=manifest.get('files');require(isinstance(rows,list) and rows,'empty_allowlist')
    seen=set()
    for row in rows:
        require(set(row)=={'path','sha256','bytes','mode','before_sha256'},'file_record_schema')
        path=row['path'];require(path not in seen,'duplicate_path');seen.add(path)
        source=safe_parent(bundle/'source',path);data,mode=regular(source)
        require(row['sha256']==sha(data) and row['bytes']==len(data)
                and row['mode']in ('0644','0755') and mode==int(row['mode'],8),'source_payload_changed:'+path)
        require(isinstance(row['before_sha256'],list) and row['before_sha256']
                and all(v is None or isinstance(v,str) and len(v)==64 and set(v)<=set('0123456789abcdef')
                        for v in row['before_sha256']),'before_pins_invalid')
        require(not data.startswith(b'\x7fELF') and b'\0'not in data,'binary_in_source_bundle')
    actual={p.relative_to(bundle/'source').as_posix()for p in (bundle/'source').rglob('*')if p.is_file()or p.is_symlink()}
    require(actual==seen,'undeclared_source_payload')
    return manifest


def inspect(upstream,manifest):
    result=subprocess.run(['git','-C',str(upstream),'rev-parse','HEAD'],capture_output=True,text=True,check=True)
    require(result.stdout.strip()==BASE,'wrong_upstream_revision')
    changed=subprocess.check_output(['git','-C',str(upstream),'diff','--name-only','-z','HEAD','--']).decode().split('\0')
    declared={row['path']for row in manifest['files']}
    require(set(changed)-{''}-declared-{GENERATED}==set(),'undeclared_tracked_edit')
    if GENERATED in changed:
        data,_=regular(safe_parent(upstream,GENERATED))
        require(data[:6]==b'\x7fELF\x02\x01'and int.from_bytes(data[18:20],'little')==183,
                'untrusted_generated_proxy_binary')
    plan=[]
    for row in manifest['files']:
        path=safe_parent(upstream,row['path'])
        before=None;mode=None
        if path.exists()or path.is_symlink():
            before,mode=regular(path);digest=sha(before)
        else:digest=None
        require(digest==row['sha256']or digest in row['before_sha256'],
                'unrecognized_source:'+row['path'])
        plan.append({'record':row,'before':before,'before_sha256':digest,'before_mode':mode,
                     'change':digest!=row['sha256']or mode!=int(row['mode'],8)})
    return plan


def atomic(path,data,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb')as stream:
            stream.write(data);stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def locked(upstream):
    root=upstream/'.xenoid';root.mkdir(mode=0o700,exist_ok=True)
    meta=root.lstat();require(stat.S_ISDIR(meta.st_mode)and meta.st_uid==os.geteuid()
                             and not meta.st_mode&0o077,'private_state_required')
    path=root/'native-source-bundle.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        meta=os.fstat(fd);require(stat.S_ISREG(meta.st_mode)and meta.st_uid==os.geteuid()
                                 and not meta.st_mode&0o077,'unsafe_lock')
        fcntl.flock(fd,fcntl.LOCK_EX);yield root
    finally:os.close(fd)


def apply(upstream,bundle,pin,*,check=False):
    upstream=Path(upstream).absolute();bundle=Path(bundle).absolute()
    require(upstream.is_dir()and not upstream.is_symlink(),'unsafe_upstream')
    manifest=load_bundle(bundle,pin)
    if check:
        plan=inspect(upstream,manifest)
        return {'ok':True,'check':True,'files':len(plan),'changes':sum(p['change']for p in plan),'manifest_sha256':pin}
    with locked(upstream)as state:
        plan=inspect(upstream,manifest)
        journal_dir=state/'native-source-bundles'/pin;journal_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
        journal=journal_dir/'journal.json'
        if journal.exists():
            value=json.loads(regular(journal)[0]);require(value.get('manifest_sha256')==pin,'journal_pin_changed')
        else:
            value={'schema':'xenoid.native-source-install/v1','manifest_sha256':pin,'completed':[]}
            for item in plan:
                if item['before']is not None:
                    name=sha(item['record']['path'].encode())+'.before'
                    atomic(journal_dir/name,item['before'],0o600)
            atomic(journal,json.dumps(value,sort_keys=True).encode(),0o600)
        for item in plan:
            row=item['record'];path=safe_parent(upstream,row['path'])
            now=regular(path)[0]if path.exists()or path.is_symlink()else None
            require((sha(now)if now is not None else None)==item['before_sha256'],'source_raced:'+row['path'])
            if item['change']:
                atomic(path,regular(bundle/'source'/row['path'])[0],int(row['mode'],8))
            data,mode=regular(path);require(sha(data)==row['sha256']and mode==int(row['mode'],8),'write_readback')
            if row['path']not in value['completed']:
                value['completed'].append(row['path']);atomic(journal,json.dumps(value,sort_keys=True).encode(),0o600)
        value['complete']=True;atomic(journal,json.dumps(value,sort_keys=True).encode(),0o600)
    return {'ok':True,'check':False,'files':len(plan),'changes':sum(p['change']for p in plan),'manifest_sha256':pin}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--upstream',type=Path,required=True)
    p.add_argument('--bundle',type=Path,required=True);p.add_argument('--manifest-sha256',required=True)
    p.add_argument('--check',action='store_true');a=p.parse_args()
    print(json.dumps(apply(a.upstream,a.bundle,a.manifest_sha256,check=a.check),sort_keys=True))


if __name__=='__main__':main()
