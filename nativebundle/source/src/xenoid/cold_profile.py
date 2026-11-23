"""Optional cold-boot template contract. No device or network operations.

An absent contract preserves the existing Raven path. A selected template is
part of the runtime-image identity and contains sentinel IDs only; real IDs
remain owned by the per-instance native identity transaction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

SCHEMA = "dev.xenoid.fingerprint/v1"
SENTINEL = "REGENERATE"
BUILD_FIELDS = (
    "brand", "manufacturer", "model", "device", "product", "board", "hardware",
    "platform", "soc_manufacturer", "soc_model", "release", "sdk", "id",
    "incremental", "description", "fingerprint", "bootloader", "security_patch",
    "first_api_level", "sku", "tags", "type", "abi", "abilist", "abilist32",
    "abilist64", "bionic_arch", "dalvik_isa_arm64", "dalvik_isa_arm",
)
RUNTIME_FIELDS = {
    "release": "13", "sdk": "33", "abi": "arm64-v8a", "abilist": "arm64-v8a",
    "abilist32": "", "abilist64": "arm64-v8a", "bionic_arch": "arm64",
    "dalvik_isa_arm64": "arm64", "dalvik_isa_arm": "", "security_patch": "2022-10-05",
}
PROFILE_ARTIFACT_OUTPUTS = {
    "libpiex_shim.so": "native/xenoid-profile-runtime/libprofile_identity.so",
    "hwcomposer.raven.so": "native/xenoid-profile-runtime/hwcomposer.profile.so",
    "xenoid-cold-seed": "native/xenoid-profile-runtime/xenoid-cold-seed",
    "libgui_pacer.so": "native/xenoid-frame-pacer/libgui_pacer.so",
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def template_from_live(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly remove all native per-instance identity values from a copy."""
    result = deepcopy(dict(profile))
    result["ids"] = {key: SENTINEL for key in
                     ("android_id", "boot_id", "serial", "imei", "imeisv")}
    result["usb"]["serial"] = SENTINEL
    return normalize_cold_profile(result)


def _positive(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("cold_profile_invalid_" + name)
    return value


def build_metadata_properties(build: Mapping[str, Any]) -> dict[str, str]:
    """Optional, profile-owned release metadata; missing fields retain legacy data.

    Date text is derived from its UTC epoch, so the framework's Build.TIME and
    every partition's text/integer build date describe the same instant.
    Values must be sourced from the selected build, never guessed here.
    """
    result: dict[str, str] = {}
    for field, prop in (("host", "ro.build.host"), ("user", "ro.build.user"),
                        ("baseband", "ro.build.expect.baseband")):
        if field not in build:
            continue
        value = build[field]
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,91}", value) is None:
            raise ValueError("cold_profile_build_metadata_invalid_" + field)
        result[prop] = value
        if field == "baseband":
            # RIL_REQUEST_BASEBAND_VERSION consumes the same expected value.
            result["gsm.version.baseband"] = value
    if "date_utc" in build:
        value = build["date_utc"]
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,11}", value) is None:
            raise ValueError("cold_profile_build_metadata_invalid_date_utc")
        try:
            stamp = datetime.fromtimestamp(int(value), timezone.utc)
        except (ValueError, OverflowError, OSError) as failure:
            raise ValueError("cold_profile_build_metadata_invalid_date_utc") from failure
        if not 1970 <= stamp.year <= 9999:
            raise ValueError("cold_profile_build_metadata_invalid_date_utc")
        day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[stamp.weekday()]
        month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[stamp.month - 1]
        text = f"{day} {month} {stamp.day:2d} {stamp:%H:%M:%S} UTC {stamp.year:04d}"
        for scope in ("", "product", "system", "system_ext", "vendor", "odm",
                      "system_dlkm", "vendor_dlkm", "odm_dlkm", "bootimage"):
            prefix = "ro.build" if not scope else "ro." + scope + ".build"
            result[prefix + ".date"] = text
            result[prefix + ".date.utc"] = str(int(value))
    return result


def normalize_cold_profile(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ValueError("cold_profile_schema_invalid")
    result = deepcopy(dict(value))
    # Canonical JSON rejects non-finite numbers and unsupported objects even in
    # metadata sections that this bootstrap does not interpret.
    if len(canonical(result)) > 256 * 1024:
        raise ValueError("cold_profile_too_large")
    for section in ("build", "usb", "input", "memory", "battery", "cpu", "display"):
        if not isinstance(result.get(section), dict):
            raise ValueError("cold_profile_section_invalid_" + section)
    build = result.get("build")
    if not isinstance(build, dict):
        raise ValueError("cold_profile_build_missing")
    build_metadata_properties(build)
    for key in BUILD_FIELDS:
        text = build.get(key)
        if (not isinstance(text, str) or any(ord(c) < 32 or ord(c) == 127 for c in text)
                or len(text.encode("utf-8")) > 91
                or (not text and key not in ("abilist32", "dalvik_isa_arm"))):
            raise ValueError("cold_profile_build_invalid_" + key)
    for key, expected in RUNTIME_FIELDS.items():
        if build[key] != expected:
            raise ValueError("cold_profile_runtime_mismatch_" + key)
    for key in ("hardware", "sku"):
        if re.fullmatch(r"[A-Za-z0-9_-]+", build[key]) is None:
            raise ValueError("cold_profile_hardware_selector_invalid")
    if build["fingerprint"] != "{brand}/{product}/{device}:{release}/{id}/{incremental}:{type}/{tags}".format(**build):
        raise ValueError("cold_profile_fingerprint_inconsistent")
    if build["description"] != "{product}-{type} {release} {id} {incremental} {tags}".format(**build):
        raise ValueError("cold_profile_description_inconsistent")
    ids = result.get("ids")
    if (not isinstance(ids, dict) or set(ids) != {"android_id", "boot_id", "serial", "imei", "imeisv"}
            or any(v != SENTINEL for v in ids.values())
            or result.get("usb", {}).get("serial") != SENTINEL):
        raise ValueError("cold_profile_must_not_contain_device_ids")
    display = result.get("display")
    if not isinstance(display, dict):
        raise ValueError("cold_profile_display_missing")
    for key in ("width", "height", "densityDpi", "physicalPpi", "defaultRefreshRateHz", "peakRefreshRateHz"):
        _positive(display.get(key), key)
    hz = display["defaultRefreshRateHz"]
    if (display["peakRefreshRateHz"] != hz or display.get("supportedRefreshRatesHz") != [hz]
            or display.get("modes") != [{"id": 1, "width": display["width"],
                                         "height": display["height"], "refreshRateHz": hz}]):
        raise ValueError("cold_profile_display_modes_inconsistent")
    for axis, dimension in (("x", "width"), ("y", "height")):
        if result.get("input", {}).get(axis) != {"minimum": 0, "maximum": display[dimension] - 1}:
            raise ValueError("cold_profile_official_touch_geometry_mismatch")
    memory = result.get("memory", {})
    if (_positive(memory.get("totalBytes"), "memory") !=
            _positive(memory.get("totalKiB"), "memory_kib") * 1024):
        raise ValueError("cold_profile_reported_memory_inconsistent")
    # Cgroup limits are deliberately not in this profile contract.
    if build["device"] == "bluejay":
        expected = {"model": "Pixel 6a", "product": "bluejay", "board": "bluejay", "hardware": "bluejay"}
        if any(build[k] != v for k, v in expected.items()):
            raise ValueError("cold_profile_bluejay_build_inconsistent")
        if any(display[k] != v for k, v in {"width": 1080, "height": 2400, "densityDpi": 420,
                "physicalPpi": 429, "defaultRefreshRateHz": 60}.items()):
            raise ValueError("cold_profile_bluejay_display_inconsistent")
        if memory["totalBytes"] != 6 * 1024 ** 3:
            raise ValueError("cold_profile_bluejay_reported_memory_inconsistent")
        if result.get("battery", {}).get("capacityMah") != 4410:
            raise ValueError("cold_profile_bluejay_battery_inconsistent")
        cpu = result.get("cpu", {})
        cores = cpu.get("cores", [])
        if (cpu.get("implementer") != "0x41" or not isinstance(cores, list)
                or len(cores) != 8 or any(not isinstance(core, dict) for core in cores)):
            raise ValueError("cold_profile_bluejay_cpu_inconsistent")
        for index, core in enumerate(cores):
            expected_cpu = (("Cortex-A55", "0xd05") if index < 4 else
                            ("Cortex-A76", "0xd0b") if index < 6 else ("Cortex-X1", "0xd44"))
            if (core.get("processor") != index or
                    (core.get("model"), core.get("part")) != expected_cpu):
                raise ValueError("cold_profile_bluejay_cpu_inconsistent")
    return result


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("cold_profile_duplicate_json_key")
        value[key] = item
    return value


def load_profile(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or not 0 < info.st_size <= 256 * 1024):
        raise ValueError("cold_profile_file_unsafe")
    return normalize_cold_profile(json.loads(path.read_bytes(), object_pairs_hook=_unique_object))


def profile_digest(profile: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(normalize_cold_profile(profile))).hexdigest()


def validate_google_profile(profile: Any, spec: Any) -> None:
    selected = normalize_cold_profile(profile)
    if selected is None or spec is None:
        return
    metadata = getattr(spec, "metadata", None)
    target = metadata.get("android", {}).get("targetProduct") if isinstance(metadata, Mapping) else None
    if target != selected["build"]["product"]:
        raise ValueError("cold_profile_google_target_mismatch")


def apply_boot_command(original: Sequence[str], profile: Any) -> list[str]:
    selected = normalize_cold_profile(profile)
    if selected is None:
        return list(original)
    build, display = selected["build"], selected["display"]
    updates = {
        "androidboot.redroid_width": display["width"],
        "androidboot.redroid_height": display["height"],
        "androidboot.redroid_dpi": display["densityDpi"],
        "androidboot.redroid_fps": display["defaultRefreshRateHz"],
        "androidboot.hardware": build["hardware"], "androidboot.hardware.sku": build["sku"],
    }
    found = {key: 0 for key in updates}
    result = []
    for argument in original:
        key = argument.partition("=")[0]
        if key in updates:
            found[key] += 1
            argument = key + "=" + str(updates[key])
        result.append(argument)
    if any(count != 1 for count in found.values()):
        raise ValueError("cold_profile_boot_keys_missing_or_duplicate")
    return result


def selected_base_profile(selected: Any, existing: Any) -> Any:
    """Use selected metadata while retaining native-owned IDs when available."""
    template = normalize_cold_profile(selected)
    if template is None:
        return existing
    if isinstance(existing, Mapping) and existing.get("schema") == SCHEMA:
        ids = existing.get("ids")
        if isinstance(ids, Mapping) and set(ids) == set(template["ids"]):
            template["ids"] = deepcopy(dict(ids))
            template["usb"]["serial"] = template["ids"]["serial"]
    return template


def expected_properties(profile: Any) -> dict[str, str]:
    selected = normalize_cold_profile(profile)
    if selected is None:
        return {}
    build = selected["build"]
    result = {
        "ro.hardware": build["hardware"], "ro.boot.hardware": build["hardware"],
        "ro.product.board": build["board"], "ro.board.platform": build["platform"],
        "ro.soc.manufacturer": build["soc_manufacturer"], "ro.soc.model": build["soc_model"],
        "ro.bootloader": build["bootloader"], "ro.product.first_api_level": build["first_api_level"],
        "ro.hardware.sku": build["sku"], "ro.boot.hardware.sku": build["sku"],
        "ro.boot.product.hardware.sku": build["sku"],
        "ro.sf.lcd_density": str(selected["display"]["densityDpi"]),
    }
    for scope in ("", "product", "system", "system_ext", "vendor", "odm", "system_dlkm", "vendor_dlkm", "odm_dlkm", "bootimage"):
        prefix = "ro.build" if not scope else "ro." + scope + ".build"
        for suffix, key in (("fingerprint", "fingerprint"), ("id", "id"), ("display.id", "id"),
                            ("version.incremental", "incremental"), ("description", "description"),
                            ("version.release", "release"), ("version.release_or_codename", "release"),
                            ("version.sdk", "sdk"), ("version.security_patch", "security_patch"),
                            ("type", "type"), ("tags", "tags")):
            result[prefix + "." + suffix] = build[key]
        if scope != "bootimage":
            product = "ro.product" if not scope else "ro.product." + scope
            for suffix, key in (("brand", "brand"), ("manufacturer", "manufacturer"),
                                ("model", "model"), ("device", "device"), ("name", "product")):
                result[product + "." + suffix] = build[key]
    result.update(build_metadata_properties(build))
    return result


def augment_partition_lines(generated: dict[str, list[str]], profile: Any) -> None:
    """Append explicit-template-only metadata after the existing generator."""
    selected = normalize_cold_profile(profile)
    if selected is None:
        return
    build, display = selected["build"], selected["display"]
    generated["system_build.prop"] += [
        "ro.sf.lcd_density=" + str(display["densityDpi"]),
        "ro.build.flavor=" + build["product"] + "-" + build["type"],
    ]
    generated["vendor_build.prop"].append("ro.boot.product.hardware.sku=" + build["sku"])
    for scope in ("bootimage",):
        for suffix, key in (("fingerprint", "fingerprint"), ("id", "id"), ("type", "type"),
                            ("tags", "tags"), ("version.incremental", "incremental"),
                            ("version.release", "release"), ("version.sdk", "sdk"),
                            ("display.id", "id"), ("description", "description"),
                            ("version.release_or_codename", "release"),
                            ("version.security_patch", "security_patch")):
            generated["system_build.prop"].append(f"ro.{scope}.build.{suffix}={build[key]}")
    for filename, lines in generated.items():
        # Full partition aliases used by native and Java readers; no wildcard
        # deletion of unrelated runtime/security properties.
        scope = filename.removesuffix("_build.prop")
        existing = {line.partition("=")[0] for line in lines}
        for name, key in (("brand", "brand"), ("manufacturer", "manufacturer"),
                          ("model", "model"), ("device", "device"), ("name", "product")):
            property_name = f"ro.{scope}.product.{name}"
            if property_name not in existing:
                lines.append(property_name + "=" + build[key])
        # The runtime helper only maps global display ID/description and two
        # security-patch aliases, so these partition values must be baked.
        for suffix, key in (("display.id", "id"), ("description", "description"),
                            ("version.security_patch", "security_patch")):
            property_name = f"ro.{scope}.build.{suffix}"
            if property_name not in existing:
                lines.append(property_name + "=" + build[key])
    # This runtime image has nested DLKM property files, without mounted
    # /system_dlkm or /odm_dlkm partitions for init to load them from. Publish
    # these otherwise-unmapped values in the normal cold-loaded system file.
    # Keep their partition copies coherent for file readers as well.
    system = generated["system_build.prop"]
    existing = {line.partition("=")[0] for line in system}
    for scope in ("system_dlkm", "odm_dlkm"):
        for suffix, key in (("display.id", "id"), ("description", "description"),
                            ("version.security_patch", "security_patch")):
            property_name = f"ro.{scope}.build.{suffix}"
            if property_name not in existing:
                system.append(property_name + "=" + build[key])
    metadata = build_metadata_properties(build)
    for filename, lines in generated.items():
        scope = filename.removesuffix("_build.prop")
        for key, value in metadata.items():
            if key.startswith("ro." + scope + ".build."):
                lines.append(key + "=" + value)
    # The normal system property loader also owns global/bootimage and nested
    # DLKM aliases. Keep partition copies above for direct file readers.
    for key, value in metadata.items():
        if not any(line.partition("=")[0] == key for line in system):
            system.append(key + "=" + value)


def prepare_context(root: Path, output: Path, profile: Any, artifact_root: Path, artifact_manifest: Mapping[str, str]) -> None:
    """Mutate only the temporary Docker context, before its manifest is sealed."""
    selected = normalize_cold_profile(profile)
    if selected is None:
        return
    expected_paths = set(PROFILE_ARTIFACT_OUTPUTS.values())
    if set(artifact_manifest) != expected_paths:
        raise ValueError("cold_profile_artifact_manifest_invalid")
    for destination_name, relative in PROFILE_ARTIFACT_OUTPUTS.items():
        source = artifact_root / relative
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise ValueError("cold_profile_staged_artifact_unsafe")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_manifest[relative]:
            raise ValueError("cold_profile_staged_artifact_changed")
        destination = output / "payload" / destination_name
        destination.write_bytes(data)
        destination.chmod(0o755 if destination_name == "xenoid-cold-seed" else 0o644)
    bluetooth_replay = root / "runtime/redroid/xenoid-bluetooth-address.sh"
    if bluetooth_replay.is_symlink() or not bluetooth_replay.is_file():
        raise ValueError("cold_profile_bluetooth_replay_missing")
    (output / "payload" / "xenoid-bluetooth-address.sh").write_bytes(bluetooth_replay.read_bytes())
    (output / "payload" / "xenoid-bluetooth-address.sh").chmod(0o644)
    (output / "payload" / "cold-profile.json").write_bytes(canonical(selected) + b"\n")
    display = selected["display"]
    mode = f"{display['width']} {display['height']} {display['physicalPpi']}\n"
    (output / "payload" / "cold-display-mode").write_text(mode)
    (output / "payload" / "cold-display-hz").write_text(str(display["defaultRefreshRateHz"]) + "\n")
    # Copy before the existing overlay stage; this must be verified to run
    # before HWC opens on the target init sequence during the cold-boot gate.
    init_path = output / "payload" / "xenoid.rc"
    if not init_path.is_file():
        raise ValueError("cold_profile_init_payload_missing")
    init_text = init_path.read_text()
    needle = "on post-fs-data\n"
    if init_text.count(needle) != 1:
        raise ValueError("cold_profile_init_anchor_changed")
    init_path.write_text(init_text.replace(needle, needle +
        "    exec_start xenoid-bluetooth-address\n"
        "    mkdir /data/local/tmp 0771 shell shell\n"
        "    copy /system/etc/xenoid-cold-display-mode /data/local/tmp/xenoid-qa-display-mode\n"
        "    chmod 0644 /data/local/tmp/xenoid-qa-display-mode\n"
        "    copy /system/etc/xenoid-cold-display-hz /data/local/tmp/xenoid-qa-refresh-rate\n"
        "    chmod 0644 /data/local/tmp/xenoid-qa-refresh-rate\n", 1) +
        "\nservice xenoid-bluetooth-address /system/bin/sh /system/etc/xenoid-bluetooth-address.sh\n"
        "    class core\n    user root\n    group root\n    seclabel u:r:init:s0\n"
        "    disabled\n    oneshot\n"
        "\non property:ro.persistent_properties.ready=true\n"
        "    exec_start xenoid-bluetooth-address\n")
    dockerfile = output / "Dockerfile"
    lines = dockerfile.read_text().splitlines()
    entries = [i for i, line in enumerate(lines) if line.startswith("ENTRYPOINT ")]
    if len(entries) != 1:
        raise ValueError("cold_profile_entrypoint_anchor_changed")
    index = entries[0]
    args = json.loads(lines[index][len("ENTRYPOINT "):])
    if args[:4] != ["/xenoid-init", "/data/xenoid-rootfs.img", "/data/xenoid-data.img", "/init"]:
        raise ValueError("cold_profile_pivot_entrypoint_changed")
    args[3:4] = ["/system/bin/xenoid-cold-seed", "--init"]
    updates = {"androidboot.hardware": selected["build"]["hardware"],
               "androidboot.hardware.sku": selected["build"]["sku"]}
    for key, value in updates.items():
        indices = [i for i, arg in enumerate(args) if arg.startswith(key + "=")]
        if len(indices) != 1:
            raise ValueError("cold_profile_entrypoint_key_changed")
        args[indices[0]] = key + "=" + value
    lines[index] = "ENTRYPOINT " + json.dumps(args, separators=(",", ":"))
    hardware = selected["build"]["hardware"]
    # Keep original aliases and add selected-family aliases, using identical
    # validated libraries. COPY runs before the final ENTRYPOINT declaration.
    lines[index:index] = [
        f"COPY --chmod=644 payload/gralloc.redroid.so /vendor/lib64/hw/gralloc.{hardware}.so",
        f"COPY --chmod=644 payload/hwcomposer.raven.so /vendor/lib64/hw/hwcomposer.{hardware}.so",
        "COPY --chmod=644 payload/cold-display-mode /system/etc/xenoid-cold-display-mode",
        "COPY --chmod=644 payload/cold-display-hz /system/etc/xenoid-cold-display-hz",
        "COPY --chmod=644 payload/cold-profile.json /system/etc/xenoid-cold-profile.json",
        "COPY --chmod=755 payload/xenoid-cold-seed /system/bin/xenoid-cold-seed",
        "COPY --chown=0:0 --chmod=0644 payload/xenoid-bluetooth-address.sh /system/etc/xenoid-bluetooth-address.sh",
    ]
    dockerfile.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    if profile_digest(profile) != args.sha256:
        raise ValueError("cold_profile_stage_hash_mismatch")
    manifest = json.loads(args.artifact_manifest.read_text())
    prepare_context(args.root, args.output, profile, args.artifact_root, manifest)


if __name__ == "__main__":
    main()
