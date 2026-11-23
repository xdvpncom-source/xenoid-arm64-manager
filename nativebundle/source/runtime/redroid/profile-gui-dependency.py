"""Add a GLOBAL helper dependency without relocating libgui code or data.

Source-controlled transform for the pinned Android 13 base. It appends a read-only segment containing a new
program-header table, dynamic table and extended string table. Original note,
relocation, symbol, code and data sections retain their virtual addresses.
"""
import argparse, hashlib, json, struct
from pathlib import Path
EXPECTED = 'b73d911a1680dd3ccc9ec4370aa3366d54f347ecaff3977f6ba58d0e1658e86c'
NEEDED = '/system/lib64/libgui_pacer.so'
def aligned(n, a=4096): return (n+a-1)//a*a

def transform(data):
    assert hashlib.sha256(data).hexdigest() == EXPECTED, 'unknown_libgui_build'
    assert data[:6] == b'\x7fELF\x02\x01', 'requires_elf64_little_endian'
    phoff, shoff = struct.unpack_from('<QQ', data, 32)
    phsize, phnum, shsize, shnum, shstr = struct.unpack_from('<HHHHH', data, 54)
    assert phsize == 56 and shsize == 64
    ph = [list(struct.unpack_from('<IIQQQQQQ', data, phoff+i*phsize)) for i in range(phnum)]
    sh = [list(struct.unpack_from('<IIQQQQIIQQ', data, shoff+i*shsize)) for i in range(shnum)]
    dynindex = next(i for i,p in enumerate(ph) if p[0] == 2)
    original_dyn = ph[dynindex]
    dyn = [list(struct.unpack_from('<QQ', data, original_dyn[2]+i)) for i in range(0, original_dyn[5], 16)]
    assert dyn[-1] == [0,0] and all(d[0] for d in dyn[:-1]), 'noncanonical_dynamic_table'
    oldstraddr = next(v for t,v in dyn if t == 5)
    oldstrlen = next(v for t,v in dyn if t == 10)
    oldstrsec = next(s for s in sh if s[1] == 3 and s[3] == oldstraddr and s[5] == oldstrlen)
    oldstrings = data[oldstrsec[4]:oldstrsec[4]+oldstrlen]
    newstrings = oldstrings + NEEDED.encode() + b'\0'
    newphnum = phnum+1
    newoff = aligned(len(data))
    newaddr = aligned(max(p[3]+p[6] for p in ph if p[0] == 1))
    newdynoff = aligned(newphnum*phsize, 16)
    newstroff = aligned(newdynoff+(len(dyn)+1)*16, 16)
    newsize = newstroff+len(newstrings)
    ph = [[6,4,newoff,newaddr,newaddr,newphnum*phsize,newphnum*phsize,8] if p[0]==6 else p for p in ph]
    ph[dynindex] = [2,4,newoff+newdynoff,newaddr+newdynoff,newaddr+newdynoff,(len(dyn)+1)*16,(len(dyn)+1)*16,8]
    ph.append([1,4,newoff,newaddr,newaddr,newsize,newsize,4096])
    newdyn = [[1,oldstrlen]] + [[t, newaddr+newstroff if t==5 else len(newstrings) if t==10 else v] for t,v in dyn]
    out = bytearray(data)
    out.extend(b'\0'*(newoff+newsize-len(out)))
    struct.pack_into('<Q',out,32,newoff);struct.pack_into('<H',out,56,newphnum)
    for i,p in enumerate(ph):struct.pack_into('<IIQQQQQQ',out,newoff+i*phsize,*p)
    for i,d in enumerate(newdyn):struct.pack_into('<QQ',out,newoff+newdynoff+i*16,*d)
    out[newoff+newstroff:newoff+newstroff+len(newstrings)] = newstrings
    for i,s in enumerate(sh):
        if s[1] == 6 and s[3] == original_dyn[3]:
            s[2] &= ~1  # new table is read-only
            s[3],s[4],s[5] = newaddr+newdynoff,newoff+newdynoff,len(newdyn)*16
        elif s is oldstrsec:
            s[3],s[4],s[5] = newaddr+newstroff,newoff+newstroff,len(newstrings)
        struct.pack_into('<IIQQQQIIQQ',out,shoff+i*shsize,*s)
    # All original executable code, symbol records and relocation bytes unchanged.
    for s in sh:
        if (s[2]&4) or s[1] in (4,11) or s[1] == 0x6fffff00:
            assert out[s[4]:s[4]+s[5]] == data[s[4]:s[4]+s[5]]
    assert len(oldstrings)==oldstrlen and out[newoff+newstroff:newoff+newstroff+oldstrlen]==oldstrings
    return bytes(out), {'needed':NEEDED,'new_segment_offset':hex(newoff),'new_segment_vaddr':hex(newaddr),'new_program_header_count':newphnum,'original_code_unchanged':True,'original_relocations_unchanged':True}

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('original',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    assert a.original.resolve()!=a.output.resolve()
    data,info=transform(a.original.read_bytes());a.output.write_bytes(data)
    info.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),original_sha256=EXPECTED)
    a.output.with_suffix('.json').write_text(json.dumps(info,indent=2)+'\n');print(json.dumps(info))
