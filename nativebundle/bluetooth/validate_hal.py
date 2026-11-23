#!/usr/bin/env python3
"""Read-only qualification of the private source-built Bluetooth service.

This emits evidence, not a deployment action or a runtime-health verdict.
The build command file must come from the target Ninja graph with response
files expanded; a one-entry-per-source compile database is insufficient.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
OLD_SHA = "961214bcb4abed4e0617d86cc3008310110adf2a8fdb33999c56745d7413e283"
PATCH_SHA = "7c2ed6b3fdcfbebcca47bd5b5d9c31e2c3506a9716c32cf8c5348295b5398cac"
BT_COMMIT = "afe918573bddb9bf4c657fd86a2fdaeb4526f9d7"
SERVICE = "android.hardware.bluetooth@1.1-service.sim"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(tool, *args):
    result = subprocess.run([str(tool), *map(str, args)], capture_output=True, text=True)
    require(result.returncode == 0, f"{Path(tool).name} failed: {result.stderr[:300]}")
    return result.stdout


def symbols(text):
    """Only externally visible defined symbols and strong undefined imports."""
    exports, imports = set(), set()
    pattern = re.compile(r"^\s*\d+:\s+[0-9a-fA-F]+\s+\d+\s+.+?\s+"
                         r"(GLOBAL|WEAK|UNIQUE)\s+(DEFAULT|PROTECTED|HIDDEN|INTERNAL)"
                         r"\s+(\S+)\s+(\S+)")
    for line in text.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        bind, vis, index, name = match.groups()
        if bind not in {"GLOBAL", "WEAK", "UNIQUE"} or vis not in {"DEFAULT", "PROTECTED"}:
            continue
        pieces = re.split(r"(@@?)", name, maxsplit=1)
        base = pieces[0]
        version = pieces[2] if len(pieces) == 3 else ""
        if index == "UND":
            if bind != "WEAK":
                imports.add((base, version))
        else:
            exports.add((base, version))
            if len(pieces) == 1 or pieces[1] == "@@":
                exports.add((base, ""))
    return exports, imports


def dynamic(text):
    return dict(needed=re.findall(r"\(NEEDED\).*?\[(.*?)\]", text),
                soname=re.findall(r"\(SONAME\).*?\[(.*?)\]", text),
                runpath=re.findall(r"\((?:RUNPATH|RPATH)\).*?\[(.*?)\]", text),
                pie=bool(re.search(r"\(FLAGS_1\).*\bPIE\b", text)))


def load_segments(path):
    data = Path(path).read_bytes()
    require(data[:6] == b"\x7fELF\x02\x01", "expected ELF64 little endian")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    require(header[0] == 3 and header[1] == 183, "expected AArch64 ET_DYN")
    phoff, phsize, phnum = header[4], header[8], header[9]
    require(phsize == 56, "unexpected ELF program-header size")
    normalized = bytearray(data)
    normalized[40:48] = bytes(8)  # Non-loaded section-table location.
    normalized[58:64] = bytes(6)  # Non-loaded section-table counts.
    segments = []
    interpreter = None
    for i in range(phnum):
        kind, flags, off, addr, _, size, memsize, align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + i * phsize)
        require(off + size <= len(data), "ELF program segment exceeds file")
        if kind == 1:
            segments.append([flags, addr, size, memsize, align,
                             hashlib.sha256(normalized[off:off + size]).hexdigest()])
        if kind == 3:
            interpreter = data[off:off + size].rstrip(b"\0").decode()
    return segments, interpreter


def inspect_elf(path, readelf):
    segments, interpreter = load_segments(path)
    dt = dynamic(run(readelf, "-dW", path))
    exports, imports = symbols(run(readelf, "--dyn-syms", "-W", path))
    note = run(readelf, "-nW", path)
    build_ids = re.findall(r"Build ID:\s*([a-fA-F0-9]+)", note)
    require(len(build_ids) == 1, "expected one GNU build ID")
    return dict(path=str(path), sha256=sha(path), bytes=Path(path).stat().st_size,
                dynamic=dt, exports=exports, imports=imports, segments=segments,
                interpreter=interpreter, build_id=build_ids[0])


def config_section(text, section):
    found = re.search(r"(?ms)^\[" + re.escape(section) + r"\]\s*\n(.*?)(?=^\[|\Z)", text)
    require(found is not None, "missing linker namespace section")
    values = {}
    for line in found.group(1).splitlines():
        m = re.match(r"\s*([^#=+]+?)\s*(\+?=)\s*(.*?)\s*$", line)
        if m:
            key, op, value = m.groups()
            values.setdefault(key, [])
            if op == "=":
                values[key] = []
            values[key].extend(value.split(":"))
    return values


def dependency_exports(runtime):
    """Select by live map, then this image's captured vendor namespace rules."""
    config = config_section(runtime["linker_config"], "vendor")
    resolved, exported = {}, set()
    for name, record in runtime["dependencies"].items():
        candidates = record["candidates"]
        require(bool(candidates), "missing dependency candidates: " + name)
        if record["resolved_by_live_mapping"]:
            require(len(candidates) == 1, "ambiguous live dependency")
            chosen = candidates[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            # Default namespace search precedes allowed linked namespaces.
            namespaces = ["default"]
            namespaces += config.get("namespace.default.links", [""])[0].split(",")
            chosen = None
            for namespace in namespaces:
                if namespace != "default" and name not in config.get(
                        f"namespace.default.link.{namespace}.shared_libs", []):
                    continue
                for directory in config.get(f"namespace.{namespace}.search.paths", []):
                    expected = directory.replace("${LIB}", "lib64") + "/" + name
                    matches = [c for c in candidates if c["path"] == expected]
                    if matches:
                        require(len(matches) == 1, "duplicate dependency path")
                        chosen = matches[0]
                        break
                if chosen:
                    break
            require(chosen is not None, "unresolved linker namespace: " + name)
        exported.update(symbols(chosen["symbols"])[0])
        resolved[name] = dict(path=chosen["path"], sha256=chosen["sha256"])
    return resolved, exported


def sanitizer_flags(argv):
    enabled, disabled = set(), set()
    for arg in argv:
        if arg.startswith("-fsanitize="):
            enabled.update(arg.split("=", 1)[1].split(","))
        elif arg.startswith("-fno-sanitize="):
            disabled.update(arg.split("=", 1)[1].split(","))
        elif arg.startswith("-fsanitize-recover="):
            recovered = arg.split("=", 1)[1].split(",")
            require(not any(x == "all" or x.startswith("cfi") for x in recovered),
                    "CFI recovery weakens fatal runtime checks")
    require(not any(x == "all" or x.startswith("cfi") for x in disabled),
            "CFI disabled in an actual target command")
    require("cfi" in enabled and "-fsanitize-cfi-cross-dso" in argv,
            "missing CFI/cross-DSO CFI in actual target command")
    require(any(x == "-flto" or x.startswith("-flto=") for x in argv), "missing LTO")
    require("-fno-sanitize-cfi-cross-dso" not in argv, "cross-DSO CFI disabled")


def validate_commands(rows, require_impl=False):
    """Rows are expanded Ninja commands, never a compile_commands.json dump."""
    needed = {"rootcanal", "service_bluetooth_hci", "service_main", "service_link", "rust_lmp"}
    if require_impl:
        needed |= {"impl_bluetooth_hci", "impl_link"}
    found, ignorelists = {}, set()
    for row in rows:
        argv = row.get("argv")
        require(isinstance(argv, list) and all(isinstance(x, str) for x in argv),
                "each command needs a tokenized argv list")
        require(not any(x.startswith("@") for x in argv), "unexpanded response file")
        if "-o" not in argv:
            continue
        output = argv[argv.index("-o") + 1]
        if "/liblmp/" in output and re.search(r"/android_vendor\.[^/]*arm64[^/]*/", output):
            require("prebuilts/rust/linux-x86/1.59.0/bin/rustc" in argv[0],
                    "unexpected Rust toolchain")
            require("aarch64-linux-android" in argv or "--target=aarch64-linux-android" in argv,
                    "unexpected Rust target")
            require("rust_lmp" not in found, "multiple Rust LMP target variants")
            found["rust_lmp"] = dict(output=output, argv_sha256=hashlib.sha256(
                json.dumps(argv, separators=(",", ":")).encode()).hexdigest())
            continue
        # Explicitly select only the ARM64 vendor CFI variant; ignore host twins.
        if not re.search(r"/android_vendor\.[^/]*arm64[^/]*cfi[^/]*/", output):
            continue
        is_service = f"/{SERVICE}/" in output
        is_impl = "/android.hardware.bluetooth@1.1-impl-sim/" in output
        is_root = "/libbt-rootcanal/" in output
        if not (is_service or is_impl or is_root):
            continue
        require(re.search(r"(?:^|/)prebuilts/clang/host/linux-x86/clang-r450784d/bin/clang(?:\+\+)?$", argv[0]),
                "unexpected Clang toolchain")
        sanitizer_flags(argv)
        ignorelists.update(x.split("=", 1)[1] for x in argv if x.startswith(
            ("-fsanitize-blacklist=", "-fsanitize-ignorelist=")))
        role = None
        if "-c" in argv:
            sources = [x for x in argv if not x.startswith("-") and
                       re.search(r"\.(?:c|cc|cpp|cxx|S|s)$", x)]
            require(len(sources) == 1, "ambiguous C/C++ compile input")
            source = sources[0]
            require("-DROOTCANAL_LMP" in argv or "-DROOTCANAL_LMP=1" in argv,
                    "class-layout macro missing: " + source)
            require(not any(x.startswith("-UROOTCANAL_LMP") for x in argv), "LMP undefined")
            require(any("aarch64" in x for x in argv if "linux-android" in x),
                    "missing AArch64 Android target")
            if is_root and source.endswith("/model/controller/link_layer_controller.cc"):
                role = "rootcanal"
            elif (is_service or is_impl) and source.endswith("/bluetooth_hci.cc"):
                role = "service_bluetooth_hci" if is_service else "impl_bluetooth_hci"
            elif is_service and source.endswith("/service.cc"):
                role = "service_main"
        elif is_service or is_impl:
            role = "service_link" if is_service else "impl_link"
        if role:
            require(role not in found, "multiple selected command variants for " + role)
            found[role] = dict(output=output, argv_sha256=hashlib.sha256(
                json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
                ignorelist_arguments=[x for x in argv if "sanitize" in x and (
                    "blacklist" in x or "ignorelist" in x)])
    require(needed <= found.keys(), "missing target commands: " + ", ".join(sorted(needed - found.keys())))
    return dict(required_commands=found, ignorelist_paths=sorted(ignorelists))


def validate_source(root, manifest_path, manifest_sha, helper, helper_sha, ignorelists=()):
    require(sha(helper) == helper_sha, "source-verifier hash changed")
    spec = importlib.util.spec_from_file_location("pinned_source_verifier", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = module.load_manifest(Path(manifest_path), manifest_sha)
    require(manifest.get("approved_patch_sha256") == PATCH_SHA, "wrong source patch")
    require(any(p["path"] == "packages/modules/Bluetooth" and p["commit"] == BT_COMMIT
                for p in manifest["source_heads"]), "Bluetooth source revision changed")
    patch = json.loads((HERE / "patch-manifest.json").read_text())
    entries = {e["path"]: e for e in manifest["entries"]}
    expected = {"packages/modules/Bluetooth/" + p for p in patch["files"]}
    require(set(manifest["approved_modified_paths"]) == expected, "unapproved modified sources")
    for rel, info in patch["files"].items():
        require(entries["packages/modules/Bluetooth/" + rel]["sha256"] == info["after_sha256"],
                "source patch byte mismatch")
    resolved_root = Path(root).resolve(strict=True)
    for name in ignorelists:
        path = Path(name) if Path(name).is_absolute() else resolved_root / name
        require(path.resolve(strict=True).is_relative_to(resolved_root), "external sanitizer ignorelist")
        rel = str(path.resolve(strict=True).relative_to(resolved_root))
        require(rel in entries and entries[rel].get("kind") == "file" and
                sha(path) == entries[rel].get("sha256"), "unattested sanitizer ignorelist: " + rel)
    module.verify(Path(root), manifest)
    return dict(manifest_sha256=manifest_sha, tree_sha256=manifest["tree_sha256"],
                projects=len(manifest["source_heads"]), patch_sha256=PATCH_SHA)


def validate(args):
    runtime = json.loads(args.runtime.read_text())
    require(runtime["service"]["sha256"] == OLD_SHA, "unexpected runtime baseline HAL")
    old = dynamic(runtime["service"]["dynamic"])
    elf = inspect_elf(args.service, args.readelf)
    full = inspect_elf(args.unstripped, args.readelf)
    require(elf["sha256"] != OLD_SHA, "unrepaired baseline supplied as new HAL")
    require(elf["dynamic"] == old, "ELF dependency/SONAME/RPATH/PIE contract changed")
    require(elf["interpreter"] == "/system/bin/linker64", "wrong interpreter")
    require(elf["build_id"] == full["build_id"] and elf["segments"] == full["segments"],
            "installed and unstripped ELF load images differ")
    require(("__cfi_check", "") in elf["exports"], "CFI entry point missing")
    require(("__cfi_slowpath", "LIBC_OMR1") in elf["imports"], "CFI slowpath missing")
    resolved, exports = dependency_exports(runtime)
    missing = elf["imports"] - exports
    require(not missing, "unresolved/version-mismatched runtime imports: " + repr(sorted(missing)[:12]))
    table = run(args.nm, "--defined-only", args.unstripped)
    wrappers = [line.split()[-1] for line in table.splitlines() if "DeleteLinkManager" in line]
    bodies = [name for name in wrappers if name.endswith(".cfi")]
    if bodies:
        require(len(bodies) == 1 and len(wrappers) <= 2, "ambiguous typed deleter")
        body = bodies[0]
    else:
        require(len(wrappers) == 1, "expected one retained typed deleter")
        body = wrappers[0]
    disassembly = run(args.objdump, "--disassemble-symbols=" + body, args.unstripped)
    require(re.search(r"\b(?:b|bl)\s+.*<link_manager_destroy>", disassembly) is not None,
            "typed deleter must directly call the Rust destroy export")
    require(re.search(r"\b(?:br|blr)\s", disassembly) is None,
            "unexpected indirect branch in typed deleter")
    commands = json.loads(args.commands.read_text())
    require(commands["origin"] == "expanded_target_ninja_commands", "Ninja command evidence required")
    command_report = validate_commands(commands["commands"], args.require_impl)
    source = validate_source(args.source_root, args.source_manifest, args.source_manifest_sha256,
                             args.source_verifier, args.source_verifier_sha256,
                             command_report["ignorelist_paths"])
    public = {k: elf[k] for k in ["path", "sha256", "bytes", "build_id", "interpreter", "dynamic"]}
    return dict(schema="xenoid.bluetooth-hal-qualification/v1", validator_sha256=sha(__file__),
                static_qualification_passed=True, cfi_retained=True,
                typed_ffi_deleter_verified=True, runtime_health_verified=False,
                deployed=False, service=public, source=source, target_commands=command_report,
                runtime_dependencies=resolved, runtime_evidence_sha256=sha(args.runtime),
                typed_deleter_symbol=body, commands_sha256=sha(args.commands),
                install_paths=["/vendor/bin/hw/" + SERVICE],
                limitations=["CFI flags and symbols alone are not a runtime HCI Reset test",
                             "fresh service mapping and source-owned image rollout remain required",
                             "sanitizer ignorelist arguments require independent source review"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ["service", "unstripped", "runtime", "commands", "source-root", "source-manifest",
                 "source-verifier", "readelf", "nm", "objdump"]:
        p.add_argument("--" + name, type=Path, required=True)
    for name in ["source-manifest-sha256", "source-verifier-sha256"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--require-impl", action="store_true",
                   help="also require the unused passthrough implementation's compile/link evidence")
    args = p.parse_args()
    try:
        print(json.dumps(validate(args), sort_keys=True))
    except (ValueError, KeyError, IndexError, OSError) as error:
        print(json.dumps(dict(static_qualification_passed=False, error=str(error))), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
