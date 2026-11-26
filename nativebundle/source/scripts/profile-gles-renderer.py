#!/usr/bin/env python3
"""Configure the standard GLES renderer string in one pinned ANGLE build.

The implementation of Context::getString(GL_RENDERER) is pinned to the public
Android ANGLE source revision below. Only its four-byte address-producing
instruction changes. An appended read-only ELF segment owns the configured
string. Original code addresses, capabilities, vendor, Vulkan properties,
symbols, relocations and CFI remain intact. This is a reported-name setting,
not a hardware GPU replacement or a certification claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile

SCHEMA = "dev.xenoid.gles-renderer/v1"
DEFAULT_LABEL = "Mali-G78"
INPUT_SHA256 = "8385182775899793a5831cd9535b07b8d993b8582e4c4bbbf58d06e828f5216d"
INPUT_SIZE = 8030848
REVISION = "b93a9f24aa3f24ff8924e73b9d2ec964906c08a7"
SOURCE = "https://android.googlesource.com/platform/external/angle/+/" + REVISION + "/src/libANGLE/Context.cpp"
CASE_VA = 0x4b6e14
CASE_BYTES = bytes.fromhex("604e5af90d000014")
TABLE_VA = 0x298382
TABLE_BYTES = bytes((0, 24, 26, 28))
PAGE = 4096
PHDR = struct.Struct("<IIQQQQQQ")


def require(condition, reason):
    if not condition:
        raise ValueError("gles_renderer_" + reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def normalize_label(value):
    if value is None:
        return None
    require(isinstance(value, str) and 1 <= len(value) <= 128
            and value == value.strip() and all(32 <= ord(c) < 127 for c in value),
            "label_invalid")
    return value


def aligned(value, alignment=PAGE):
    return (value + alignment - 1) // alignment * alignment


def headers(raw):
    require(len(raw) >= 64, "elf_truncated")
    offset = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    require(size == PHDR.size and 0 < count < 128
            and offset + count * size <= len(raw), "program_headers_invalid")
    return offset, [PHDR.unpack_from(raw, offset + i * size) for i in range(count)]


def address_offset(address, entries):
    matches = [p[2] + address - p[3] for p in entries
               if p[0] == 1 and p[3] <= address < p[3] + p[5]]
    require(len(matches) == 1, "address_mapping_invalid")
    return matches[0]


def validate_original(raw):
    require(len(raw) == INPUT_SIZE and digest(raw) == INPUT_SHA256, "unknown_original_build")
    require(raw[:16] == bytes.fromhex("7f454c46020101000000000000000000")
            and struct.unpack_from("<HHI", raw, 16) == (3, 183, 1), "elf_identity_changed")
    offset, entries = headers(raw)
    require(offset == 64 and len(entries) == 10
            and sum(p[0] == 6 for p in entries) == 1, "original_headers_changed")
    site, table = address_offset(CASE_VA, entries), address_offset(TABLE_VA, entries)
    require(site == CASE_VA and raw[site:site + 8] == CASE_BYTES, "renderer_case_changed")
    require(raw[table:table + 4] == TABLE_BYTES
            and 0x4b6db4 + raw[table + 1] * 4 == CASE_VA, "string_dispatch_changed")
    return entries


def adrp_x0(pc, target):
    require(target % PAGE == 0, "label_alignment_invalid")
    pages = target // PAGE - pc // PAGE
    require(-(1 << 20) <= pages < (1 << 20), "label_address_out_of_range")
    immediate = pages & ((1 << 21) - 1)
    return 0x90000000 | ((immediate & 3) << 29) | ((immediate >> 2) << 5)


def transform(original, label=DEFAULT_LABEL):
    label = normalize_label(label)
    old = validate_original(original)
    metadata = {"schema": SCHEMA, "enabled": label is not None, "label": label,
                "original_sha256": INPUT_SHA256, "original_size": len(original),
                "angle_revision": REVISION, "source": SOURCE,
                "reported_name_only": True, "physical_gpu_changed": False}
    if label is None:
        candidate = original
    else:
        text = label.encode("ascii") + b"\0"
        load_offset = aligned(len(original))
        load_address = aligned(max(p[3] + p[6] for p in old if p[0] == 1))
        require((load_offset, load_address) == (0x7a9000, 0x79f000), "mapping_layout_changed")
        table_relative = aligned(len(text), 8)
        table_size = (len(old) + 1) * PHDR.size
        phoff, phaddr = load_offset + table_relative, load_address + table_relative
        payload_size = table_relative + table_size
        new = [(6, 4, phoff, phaddr, phaddr, table_size, table_size, 8)
               if p[0] == 6 else p for p in old]
        new.append((1, 4, load_offset, load_address, load_address,
                    payload_size, payload_size, PAGE))
        result = bytearray(original)
        result.extend(b"\0" * (load_offset + payload_size - len(result)))
        result[load_offset:load_offset + len(text)] = text
        for index, values in enumerate(new):
            PHDR.pack_into(result, phoff + index * PHDR.size, *values)
        struct.pack_into("<Q", result, 32, phoff)
        struct.pack_into("<H", result, 56, len(new))
        struct.pack_into("<I", result, CASE_VA, adrp_x0(CASE_VA, load_address))
        candidate = bytes(result)
    metadata.update(output_sha256=digest(candidate), output_size=len(candidate),
                    original_build_id_retained=True)
    verify(original, candidate, metadata)
    return candidate, metadata


def verify(original, candidate, metadata):
    """Independent structural verification; never trusts the output manifest."""
    old = validate_original(original)
    label = normalize_label(metadata.get("label"))
    require(metadata.get("schema") == SCHEMA
            and metadata.get("enabled") is (label is not None)
            and metadata.get("original_sha256") == INPUT_SHA256
            and metadata.get("original_size") == len(original)
            and metadata.get("output_sha256") == digest(candidate)
            and metadata.get("output_size") == len(candidate), "manifest_invalid")
    if label is None:
        require(candidate == original, "disabled_bytes_changed")
        return {"ok": True, "enabled": False, "output_sha256": digest(candidate)}
    text = label.encode("ascii") + b"\0"
    load_offset, load_address = 0x7a9000, 0x79f000
    table_relative = aligned(len(text), 8)
    table_size = 11 * PHDR.size
    phoff, phaddr = load_offset + table_relative, load_address + table_relative
    payload_size = table_relative + table_size
    offset, new = headers(candidate)
    expected_phdr = (6, 4, phoff, phaddr, phaddr, table_size, table_size, 8)
    appended = (1, 4, load_offset, load_address, load_address, payload_size, payload_size, PAGE)
    require(offset == phoff and new == [expected_phdr if p[0] == 6 else p for p in old] + [appended],
            "program_header_records_changed")
    require(len(candidate) == load_offset + payload_size
            and candidate[len(original):load_offset] == b"\0" * (load_offset - len(original))
            and candidate[load_offset:phoff] == text + b"\0" * (table_relative - len(text)),
            "appended_payload_changed")
    word = struct.unpack_from("<I", candidate, CASE_VA)[0]
    require(word & 0x9f00001f == 0x90000000, "instruction_not_adrp_x0")
    immediate = ((word >> 29) & 3) | (((word >> 5) & 0x7ffff) << 2)
    if immediate & (1 << 20):
        immediate -= 1 << 21
    require((CASE_VA & ~(PAGE - 1)) + immediate * PAGE == load_address, "instruction_target_changed")
    restored = bytearray(candidate[:len(original)])
    for first, last in ((32, 40), (56, 58), (CASE_VA, CASE_VA + 4)):
        restored[first:last] = original[first:last]
    require(bytes(restored) == original, "original_byte_whitelist_failed")
    ranges = []
    for p in new:
        if p[0] != 1:
            continue
        require(p[5] <= p[6] and p[2] + p[5] <= len(candidate)
                and p[7] > 0 and p[2] % p[7] == p[3] % p[7], "load_bounds_invalid")
        begin, end = p[3] // PAGE * PAGE, aligned(p[3] + p[6])
        require(all(end <= low or begin >= high for low, high in ranges), "load_pages_overlap")
        ranges.append((begin, end))
    return {"ok": True, "enabled": True, "output_sha256": digest(candidate),
            "original_code_bytes_changed": 4, "other_original_bytes_preserved": True}


def regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
            and info.st_nlink == 1, "input_file_unsafe")
    raw = path.read_bytes()
    after = path.stat()
    require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) ==
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns), "input_changed")
    return raw


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "output_symlink")
    fd, temporary = tempfile.mkstemp(prefix=".gles-renderer-", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--label", default=DEFAULT_LABEL)
    selection.add_argument("--native", action="store_true")
    args = parser.parse_args()
    require(len({p.resolve() for p in (args.original, args.output, args.manifest)}) == 3,
            "paths_must_differ")
    result, metadata = transform(regular(args.original), None if args.native else args.label)
    atomic(args.output, result)
    atomic(args.manifest, (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode())
    print(json.dumps({"ok": True, **metadata}, sort_keys=True))


if __name__ == "__main__":
    main()
