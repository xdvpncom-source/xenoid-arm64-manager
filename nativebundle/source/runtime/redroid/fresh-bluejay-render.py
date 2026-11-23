#!/usr/bin/env python3
"""Build the cold Bluejay render payload from exact base bytes and source assets.

No live container operation. The image builder extracts base files; this helper
only edits its invocation-owned context. Swift executable bytes/CFI are untouched.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import struct
import sys

sys.dont_write_bytecode = True

SCHEMA='xenoid.fresh-bluejay-render/v1'
SWIFT_SHA='da5910aeabe9ee9c9559dd49f497c741513e856ea34af2626deabc75b7966a90'
SWIFT_SIZE=19805584
SWIFT_OFFSET=90269
SWIFT_OLD=b'SwiftShader.ini\0'
SWIFT_NEW=b'/vendor/sw.ini\0\0'
CONFIG=b'[Processor]\nThreadCount=1\n'
PROPERTIES={'debug.hwc.present_fps':'15','debug.gui.frame_pacer_fps':'15','debug.hwui.fps_divisor':'4'}
DISPLAY={'width':1080,'height':2400,'densityDpi':420,'physicalPpi':429}


def require(value,code):
    if not value: raise ValueError('fresh_bluejay_render_'+code)


def digest(data): return hashlib.sha256(data).hexdigest()


def regular(path):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(),'regular_file_required')
    return path.read_bytes()


def swift_config_path(data):
    require(len(data)==SWIFT_SIZE and digest(data)==SWIFT_SHA,'swift_base_changed')
    require(len(SWIFT_OLD)==len(SWIFT_NEW)==16,'swift_literal_size')
    require(data.count(SWIFT_OLD)==1 and data[SWIFT_OFFSET:SWIFT_OFFSET+16]==SWIFT_OLD,
            'swift_literal_changed')
    require(data[:6]==b'\x7fELF\x02\x01','swift_elf')
    phoff=struct.unpack_from('<Q',data,32)[0]
    phsize,phnum=struct.unpack_from('<HH',data,54)
    require(phsize==56 and 0<phnum<128,'swift_program_headers')
    loads=[]
    for index in range(phnum):
        kind,flags,offset,address,physical,filesz,memsz,align=struct.unpack_from('<IIQQQQQQ',data,phoff+index*phsize)
        if kind==1 and offset<=SWIFT_OFFSET and SWIFT_OFFSET+16<=offset+filesz:
            loads.append(flags)
    require(loads==[4],'swift_literal_not_readonly')
    result=data[:SWIFT_OFFSET]+SWIFT_NEW+data[SWIFT_OFFSET+16:]
    require(len(result)==len(data) and result[:SWIFT_OFFSET]==data[:SWIFT_OFFSET]
            and result[SWIFT_OFFSET+16:]==data[SWIFT_OFFSET+16:],'swift_nonliteral_change')
    return result


def prepare(context,profile,gui_transform):
    if profile['build']['device']!='bluejay': return None
    require({k:profile['display'].get(k) for k in DISPLAY}==DISPLAY and
            profile['display'].get('defaultRefreshRateHz')==60,'profile_display')
    payload=context/'payload'
    require(context.is_dir() and not context.is_symlink() and payload.is_dir()
            and not payload.is_symlink(),'context_directory')
    gui_original=regular(payload/'libgui.base.so')
    swift_original=regular(payload/'swiftshader.base.so')
    gui,gui_info=gui_transform(gui_original)
    swift=swift_config_path(swift_original)
    sources={
        'profile-libgui.so':(gui,'/system/lib64/libgui.so'),
        'profile-swiftshader.so':(swift,'/vendor/lib64/hw/vulkan.pastel.so'),
        'profile-swiftshader.ini':(CONFIG,'/vendor/etc/SwiftShader.ini'),
        'profile-swiftshader-short.ini':(CONFIG,'/vendor/sw.ini'),
    }
    artifacts={}
    for name,(data,target) in sources.items():
        path=payload/name;require(not path.exists(),'output_exists')
        path.write_bytes(data);path.chmod(0o644);artifacts[target]=digest(data)
    for source,target in (
        ('libgui_pacer.so','/system/lib64/libgui_pacer.so'),
        ('hwcomposer.raven.so','/vendor/lib64/hw/hwcomposer.bluejay.so'),
        ('libpiex_shim.so','/system/lib64/libpiex_shim.so')):
        artifacts[target]=digest(regular(payload/source))
    contract={'schema':SCHEMA,'artifactSha256':artifacts,'properties':PROPERTIES,
              'display':DISPLAY,'advertised_hz':60,'render_fps':15,'swiftshader_threads':1,
              'regular_config_files':['/vendor/etc/SwiftShader.ini','/vendor/sw.ini'],
              'source':{'gui_base_sha256':digest(gui_original),'swift_base_sha256':SWIFT_SHA,
                        'swift_transform':'readonly-config-literal-v1',
                        'gui_transform':'readonly-needed-segment-v1',
                        'swift_code_and_cfi_unchanged':True,
                        'gui_code_and_relocations_unchanged':gui_info['original_code_unchanged'] is True
                            and gui_info['original_relocations_unchanged'] is True}}
    (payload/'fresh-bluejay-render.json').write_text(json.dumps(contract,sort_keys=True,separators=(',',':'))+'\n')
    (payload/'fresh-bluejay-render.json').chmod(0o644)
    init=payload/'xenoid.rc';text=regular(init).decode()
    anchor='on post-fs-data\n'
    require(text.count(anchor)==1,'init_anchor')
    text=text.replace(anchor,anchor+''.join('    setprop '+k+' '+v+'\n' for k,v in PROPERTIES.items()),1)
    init.write_text(text)
    dockerfile=context/'Dockerfile';text=regular(dockerfile).decode()
    entries=[line for line in text.splitlines() if line.startswith('ENTRYPOINT ')]
    require(len(entries)==1,'entrypoint_anchor')
    copies='\n'.join([
        'COPY --chown=0:0 --chmod=0644 payload/profile-libgui.so /system/lib64/libgui.so',
        'COPY --chown=0:0 --chmod=0644 payload/libgui_pacer.so /system/lib64/libgui_pacer.so',
        'COPY --chown=0:0 --chmod=0644 payload/profile-swiftshader.so /vendor/lib64/hw/vulkan.pastel.so',
        'COPY --chown=0:0 --chmod=0644 payload/profile-swiftshader.ini /vendor/etc/SwiftShader.ini',
        'COPY --chown=0:0 --chmod=0644 payload/fresh-bluejay-render.json /system/etc/xenoid/fresh-bluejay-render.json',
        'COPY --chown=0:0 --chmod=0644 payload/profile-swiftshader-short.ini /vendor/sw.ini',
    ])+'\n'
    dockerfile.write_text(text.replace(entries[0],copies+entries[0],1))
    # Original context paths are not COPY inputs and must not accidentally ship.
    (payload/'libgui.base.so').unlink();(payload/'swiftshader.base.so').unlink()
    return contract


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context',type=Path,required=True);p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--profile-sha256',required=True);a=p.parse_args()
    raw=regular(a.profile);require(digest(raw)==a.profile_sha256,'profile_input_changed')
    path=Path(__file__).with_name('profile-gui-dependency.py')
    spec=importlib.util.spec_from_file_location('profile_gui_dependency',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=prepare(a.context,json.loads(raw),module.transform)
    print(json.dumps({'ok':True,'configured':result is not None}))


if __name__=='__main__':main()
