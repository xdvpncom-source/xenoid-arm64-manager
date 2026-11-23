#!/usr/bin/env python3
"""Validate/install the pinned public HAL asset into the native source tree.

No download, image build, Android command, service restart or binary patch.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import sys

sys.dont_write_bytecode = True

SERVICE='android.hardware.bluetooth@1.1-service.sim'
REQUIRED={'bluetooth/'+n for n in ('manifest.json','qualification.json',SERVICE)}


def require(ok,code):
    if not ok:raise RuntimeError('native_artifacts_'+code)


def sha(data):return hashlib.sha256(data).hexdigest()


def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def install(upstream,bundle,archive,manifest_pin,artifacts_pin):
    bundle=Path(bundle).absolute();upstream=Path(upstream).absolute();archive=Path(archive).absolute()
    owner=load_module(bundle/'apply.py','native_source_bundle')
    manifest=owner.load_bundle(bundle,manifest_pin)
    descriptor,_=owner.regular(bundle/'artifacts.json')
    require(sha(descriptor)==artifacts_pin,'descriptor_pin')
    descriptor=json.loads(descriptor)
    require(descriptor.get('schema')=='dev.xenoid.fleet.native-artifacts/v1','descriptor_schema')
    require(archive.name==descriptor.get('file'),'archive_basename')
    payload,_=owner.regular(archive)
    require(len(payload)==descriptor.get('bytes') and sha(payload)==descriptor.get('sha256'),'archive_digest')
    helper_path=upstream/'runtime/redroid/source-built-bluetooth.py'
    row=next(r for r in manifest['files'] if r['path']=='runtime/redroid/source-built-bluetooth.py')
    require(sha(owner.regular(helper_path)[0])==row['sha256'],'source_helper_pin')
    helper=load_module(helper_path,'source_built_bluetooth')
    with owner.locked(upstream) as state:
        require(all(not item['change'] for item in owner.inspect(upstream,manifest)),
                'complete_source_bundle_required')
        with tempfile.TemporaryDirectory(prefix='native-assets-',dir=state)as temporary:
            stage=Path(temporary);seen=set();total=0
            with tarfile.open(archive,'r:xz')as tar:
                for member in tar:
                    name=member.name;parts=Path(name).parts
                    require(member.isreg() and name not in seen and not Path(name).is_absolute()
                            and parts and '..'not in parts and '.'not in parts,'unsafe_archive_member')
                    require(name in REQUIRED or name.startswith(('licenses/','provenance/')),'undeclared_archive_member')
                    total+=member.size;require(0<=member.size<=8*1024*1024 and total<=32*1024*1024,'archive_expansion_limit')
                    data=tar.extractfile(member).read();require(len(data)==member.size,'archive_truncated')
                    path=stage/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    with path.open('xb')as stream:stream.write(data);os.fchmod(stream.fileno(),0o600)
                    seen.add(name)
            require(REQUIRED<=seen,'missing_release_member')
            service,_=helper.load_release(stage/'bluetooth')
            require(sha(service)==descriptor.get('service_sha256'),'service_descriptor_mismatch')
            destination=upstream/'runtime/redroid/.source-built-bluetooth'
            if destination.exists()or destination.is_symlink():
                require(destination.is_dir()and not destination.is_symlink(),'unsafe_existing_release')
                helper.load_release(destination)
                require(all(owner.regular(destination/Path(name).name)[0]==
                            owner.regular(stage/name)[0] for name in REQUIRED),'different_existing_release')
                return {'ok':True,'reused':True,'service_sha256':descriptor['service_sha256']}
            destination.parent.mkdir(parents=True,exist_ok=True)
            os.rename(stage/'bluetooth',destination)
            fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            helper.load_release(destination)
    return {'ok':True,'reused':False,'service_sha256':descriptor['service_sha256']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('archive','upstream','bundle'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--manifest-sha256',required=True);p.add_argument('--artifacts-sha256',required=True)
    a=p.parse_args();print(json.dumps(install(a.upstream,a.bundle,a.archive,a.manifest_sha256,a.artifacts_sha256),sort_keys=True))


if __name__=='__main__':main()
