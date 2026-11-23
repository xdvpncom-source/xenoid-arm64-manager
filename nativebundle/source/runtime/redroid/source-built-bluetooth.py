#!/usr/bin/env python3
"""Stage one reviewed source-built HAL into a private image build context.

This helper never executes a HAL, edits an ELF, invokes Docker, changes an
Android service, or builds a library. The producer must first run the pinned
validate_hal.py against the full build/source evidence. This consumes that
successful qualification receipt and independently checks the packaged ELF.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct

SCHEMA = "xenoid.bluetooth-source-release/v1"
QUALIFICATION_SCHEMA = "xenoid.bluetooth-hal-qualification/v1"
VALIDATOR_SHA = "7c0a6567a3ae5010e79c893c723f6ea79d29c6c2aeb485ac76ea43f5e034f750"
UPSTREAM_COMMIT = "afe918573bddb9bf4c657fd86a2fdaeb4526f9d7"
PATCH_SHA = "7c2ed6b3fdcfbebcca47bd5b5d9c31e2c3506a9716c32cf8c5348295b5398cac"
BASELINE_SHA = "961214bcb4abed4e0617d86cc3008310110adf2a8fdb33999c56745d7413e283"
RUNTIME_CONTRACT_SHA = "765be4490c1d564d8b45e8995ecec4fc84df4ab64257666afc740443b8fd4e78"
SERVICE = "android.hardware.bluetooth@1.1-service.sim"
INSTALL = "/vendor/bin/hw/" + SERVICE
NEEDED = ["android.hardware.bluetooth@1.0.so", "android.hardware.bluetooth@1.1.so",
          "libbase.so", "libhidlbase.so", "libjsoncpp.so", "libutils.so",
          "libprotobuf-cpp-lite-3.9.1.so", "libcutils.so", "liblog.so", "libc++.so",
          "libc.so", "libm.so", "libdl.so"]
COPY_RULES = ("COPY --chown=0:2000 --chmod=0755 payload/" + SERVICE + " " + INSTALL + "\n"
              "COPY --chown=0:0 --chmod=0644 payload/bluetooth-source.json "
              "/system/etc/xenoid/bluetooth-source.json")


def require(condition, code):
    if not condition:
        raise ValueError("bluetooth_source_" + code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def pairs(rows):
    value = {}
    for key, item in rows:
        require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def json_value(data):
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def sha_value(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def read_regular(path, limit):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            before.st_uid == os.getuid() and not stat.S_IMODE(before.st_mode) & 0o022,
            "unsafe_file")
    require(before.st_size <= limit, "file_too_large")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), "file_raced")
        data = bytearray()
        while True:
            part = os.read(fd, min(1024 * 1024, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
            require(len(data) <= limit, "file_too_large")
        after = os.fstat(fd)
        require((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file_raced")
        return bytes(data)
    finally:
        os.close(fd)


def inspect_elf(data):
    require(len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01", "elf_header")
    h = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    require(h[0:3] == (3, 183, 1) and h[7] == 64 and h[8] == 56 and h[10] == 64,
            "elf_architecture")
    require(0 < h[9] <= 128 and 0 < h[11] <= 1024, "elf_tables")

    def region(offset, size):
        require(offset >= 0 and size >= 0 and offset + size <= len(data), "elf_bounds")
        return data[offset:offset + size]

    interpreter, dynamic_bytes, loads = None, None, []
    for i in range(h[9]):
        fields = struct.unpack("<IIQQQQQQ", region(h[4] + i * 56, 56))
        kind, flags, offset, address, _, size, _, _ = fields
        payload = region(offset, size)
        if kind == 1:
            loads.append((address, offset, size))
        elif kind == 2:
            require(dynamic_bytes is None, "multiple_dynamic_segments")
            dynamic_bytes = payload
        elif kind == 3:
            require(interpreter is None, "multiple_interpreters")
            interpreter = payload.rstrip(b"\0").decode("ascii")
    require(interpreter == "/system/bin/linker64" and dynamic_bytes is not None, "elf_loader")
    sections = [struct.unpack("<IIQQQQIIQQ", region(h[5] + i * 64, 64)) for i in range(h[11])]
    dynsym = [s for s in sections if s[1] == 11]
    require(len(dynsym) == 1, "elf_dynsym")
    table = dynsym[0]
    require(table[9] == 24 and table[5] % 24 == 0 and table[6] < len(sections), "elf_symbols")
    strtab = sections[table[6]]
    require(strtab[1] == 3, "elf_strings")
    strings = region(strtab[4], strtab[5])

    def string(offset):
        require(0 <= offset < len(strings), "elf_string_bounds")
        end = strings.find(b"\0", offset)
        require(end >= 0, "elf_string_termination")
        return strings[offset:end].decode("utf-8", errors="strict")

    exports, imports = set(), set()
    for offset in range(table[4], table[4] + table[5], 24):
        name, info, other, section, _, _ = struct.unpack("<IBBHQQ", region(offset, 24))
        if info >> 4 not in {1, 2} or other & 3 not in {0, 3}:
            continue
        (exports if section else imports).add(string(name))
    require("__cfi_check" in exports and "__cfi_slowpath" in imports, "cfi_missing")
    require(len(dynamic_bytes) % 16 == 0, "elf_dynamic_size")
    tags = []
    for offset in range(0, len(dynamic_bytes), 16):
        tag, value = struct.unpack_from("<qQ", dynamic_bytes, offset)
        if tag == 0:
            break
        tags.append((tag, value))
    require([string(value) for tag, value in tags if tag == 1] == NEEDED, "elf_dependencies")
    require(not any(tag in {14, 15, 29} for tag, _ in tags), "elf_soname_or_runpath")
    require(any(tag == 0x6ffffffb and value & 0x08000000 for tag, value in tags), "elf_not_pie")
    build_ids = []
    for section in sections:
        if section[1] != 7:
            continue
        notes = region(section[4], section[5])
        offset = 0
        while offset < len(notes):
            require(offset + 12 <= len(notes), "elf_note")
            namesz, descsz, kind = struct.unpack_from("<III", notes, offset)
            offset += 12
            name = notes[offset:offset + namesz]
            offset += (namesz + 3) & ~3
            descriptor = notes[offset:offset + descsz]
            offset += (descsz + 3) & ~3
            require(offset <= len(notes), "elf_note_bounds")
            if kind == 3 and name == b"GNU\0":
                build_ids.append(descriptor.hex())
    require(len(build_ids) == 1 and 16 <= len(build_ids[0]) <= 128, "elf_build_id")
    return {"build_id": build_ids[0], "needed": NEEDED}


def load_release(root):
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
            not stat.S_IMODE(info.st_mode) & 0o022, "unsafe_directory")
    require({p.name for p in root.iterdir()} == {"manifest.json", "qualification.json", SERVICE},
            "release_files")
    manifest_bytes = read_regular(root / "manifest.json", 128 * 1024)
    manifest = json_value(manifest_bytes)
    require(manifest_bytes == canonical(manifest), "manifest_not_canonical")
    require(set(manifest) == {"schema", "upstream_commit", "patch_sha256", "validator_sha256",
                              "qualification_sha256", "source_manifest_sha256", "source_tree_sha256",
                              "baseline_sha256", "runtime_contract_sha256", "service"}, "manifest_fields")
    require(manifest["schema"] == SCHEMA and manifest["upstream_commit"] == UPSTREAM_COMMIT and
            manifest["patch_sha256"] == PATCH_SHA and manifest["validator_sha256"] == VALIDATOR_SHA and
            manifest["baseline_sha256"] == BASELINE_SHA and
            manifest["runtime_contract_sha256"] == RUNTIME_CONTRACT_SHA, "unsupported_provenance")
    for key in ["qualification_sha256", "source_manifest_sha256", "source_tree_sha256"]:
        require(sha_value(manifest[key]), "manifest_hash")
    spec = manifest["service"]
    require(isinstance(spec, dict) and set(spec) == {"sha256", "bytes", "build_id"}, "service_spec")
    require(sha_value(spec["sha256"]) and spec["sha256"] != BASELINE_SHA and
            type(spec["bytes"]) is int and 0 < spec["bytes"] <= 64 * 1024 * 1024,
            "service_identity")
    require(isinstance(spec["build_id"], str), "service_build_id")
    report_bytes = read_regular(root / "qualification.json", 2 * 1024 * 1024)
    require(digest(report_bytes) == manifest["qualification_sha256"], "qualification_hash")
    report = json_value(report_bytes)
    require(report.get("schema") == QUALIFICATION_SCHEMA and
            report.get("validator_sha256") == VALIDATOR_SHA and
            report.get("static_qualification_passed") is True and
            report.get("cfi_retained") is True and report.get("typed_ffi_deleter_verified") is True and
            report.get("deployed") is False, "qualification_not_passed")
    qualified = report["service"]
    require(all(qualified[key] == spec[key] for key in spec) and
            qualified["interpreter"] == "/system/bin/linker64" and
            qualified["dynamic"] == {"needed": NEEDED, "soname": [], "runpath": [], "pie": True},
            "qualification_service_mismatch")
    require(report["source"]["manifest_sha256"] == manifest["source_manifest_sha256"] and
            report["source"]["tree_sha256"] == manifest["source_tree_sha256"] and
            report["source"]["patch_sha256"] == PATCH_SHA and
            report["runtime_evidence_sha256"] == RUNTIME_CONTRACT_SHA and
            report["install_paths"] == [INSTALL], "qualification_source_mismatch")
    commands = report["target_commands"]["required_commands"]
    require({"rootcanal", "service_bluetooth_hci", "service_main", "service_link", "rust_lmp"}
            <= set(commands), "qualification_commands_missing")
    for value in commands.values():
        require(sha_value(value["argv_sha256"]), "qualification_command_hash")
    require(sha_value(report["commands_sha256"]) and bool(report.get("typed_deleter_symbol")),
            "qualification_commands_missing")
    payload = read_regular(root / SERVICE, 64 * 1024 * 1024)
    require(digest(payload) == spec["sha256"] and len(payload) == spec["bytes"], "payload_hash")
    actual = inspect_elf(payload)
    require(actual["build_id"] == spec["build_id"], "payload_build_id")
    provenance = dict(schema=SCHEMA, upstream_commit=UPSTREAM_COMMIT, patch_sha256=PATCH_SHA,
                      validator_sha256=VALIDATOR_SHA, source_manifest_sha256=manifest["source_manifest_sha256"],
                      source_tree_sha256=manifest["source_tree_sha256"],
                      qualification_sha256=manifest["qualification_sha256"],
                      release_manifest_sha256=digest(manifest_bytes), service=spec,
                      baseline_sha256=BASELINE_SHA, runtime_contract_sha256=RUNTIME_CONTRACT_SHA,
                      install=dict(path=INSTALL, uid=0, gid=2000, mode="0755"), cfi_retained=True)
    return payload, canonical(provenance)


def stage(root, context, baseline):
    payload, provenance = load_release(root)
    require(digest(read_regular(baseline, 64 * 1024 * 1024)) == BASELINE_SHA, "baseline_changed")
    for directory in [context, context / "payload"]:
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not directory.is_symlink() and
                info.st_uid == os.getuid(), "unsafe_context")
    for name, data, mode in [(SERVICE, payload, 0o755), ("bluetooth-source.json", provenance, 0o644)]:
        destination = context / "payload" / name
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
    return COPY_RULES


def enabled(root):
    try:
        root.lstat()
    except FileNotFoundError:
        return False
    load_release(root)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["enabled", "check", "stage"])
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "enabled":
            print("1" if enabled(args.assets) else "0")
        elif args.action == "check":
            load_release(args.assets)
        else:
            require(args.context is not None and args.baseline is not None, "stage_arguments")
            print(stage(args.assets, args.context, args.baseline))
    except (ValueError, KeyError, TypeError, OSError, struct.error) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
