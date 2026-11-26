"""Durable profile phase after native GEN, before Google/cache post-processing.

The official native CLI handler is called in-process under its existing lock.
This module owns no identity-reset operation and never creates a second GEN.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time

from qa_device_profile import DISPLAY_PRESET_FIELDS, canonical_bytes, display_random_candidates, field_catalog, generate_profile, profile_sha256, selected_fields, validate_policy, validate_profile

CONFIG = Path("/etc/xenoid-fleet/device-profile-policy.json")
RESOURCE_CONFIG = Path("/etc/xenoid-fleet/qa-resources.json")
STATE = Path("/var/lib/xenoid-fleet/device-profiles")
PLAN_SCHEMA = "dev.xenoid.profile-rotation-plan/v1"
STATE_SCHEMA = "dev.xenoid.applied-profile/v1"
PENDING_SCHEMA = "dev.xenoid.pending-profile/v1"
EFFECTIVE_PROFILE = "/data/local/tmp/xenoid-profile/effective.json"
_INSTANCE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BUILD_PROPERTIES = {"model": "ro.product.model", "device": "ro.product.device", "product": "ro.product.name",
                    "board": "ro.product.board", "id": "ro.build.id", "incremental": "ro.build.version.incremental",
                    "fingerprint": "ro.build.fingerprint", "description": "ro.build.description"}


class ProfilePending(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022 or info.st_size > 1024 * 1024:
        raise ProfilePending("profile_file_identity_invalid")
    with path.open("rb") as stream:
        return json.load(stream)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise ProfilePending("profile_state_directory_invalid")
    data = canonical_bytes(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _budget(broker, deadline):
    if broker.stopping.is_set():
        raise ProfilePending("script_stopping")
    if deadline is not None and time.monotonic() >= deadline:
        raise ProfilePending("profile_operation_timeout")


def _execute(broker, manager, args, deadline):
    _budget(broker, deadline)
    result = broker.native_display_exec(manager, args, deadline=deadline, check=False)
    if result.returncode:
        raise ProfilePending("profile_readback_unavailable")
    return result.stdout.decode(errors="strict").strip()


def _effective(broker, manager, deadline):
    return validate_profile(json.loads(_execute(broker, manager, ["cat", EFFECTIVE_PROFILE], deadline)))


def _display(broker, manager, deadline):
    size = _execute(broker, manager, ["wm", "size"], deadline)
    density = _execute(broker, manager, ["wm", "density"], deadline)
    def selected(text, kind, override=False):
        label = "Override" if override else "Physical"
        pattern = label + (r" size:\s*(\d+)x(\d+)" if kind == "size" else r" density:\s*(\d+)")
        match = re.search(pattern, text)
        if not match:
            if override: return None
            raise ProfilePending("profile_display_unavailable")
        values = tuple(map(int, match.groups()))
        return values if kind == "size" else values[0]
    physical_size, physical_density = selected(size, "size"), selected(density, "density")
    return {"physical_size": list(physical_size), "physical_density": physical_density,
            "size": list(selected(size, "size", True) or physical_size),
            "density": selected(density, "density", True) or physical_density}


def _display_period(broker, manager, deadline):
    text = _execute(broker, manager, ["dumpsys", "SurfaceFlinger", "--latency"], deadline)
    first = text.splitlines()[0] if text else ""
    if not first.isdigit() or int(first) <= 0:
        raise ProfilePending("profile_display_period_unavailable")
    return int(first)


def _properties(text):
    return dict(re.findall(r"(?m)^\[([^\]\n]+)\]: \[([^\n]*)\]$", text))


def _property_capacities(properties):
    """Conservative capacity inferred read-only, never run the property patcher."""
    result = {}
    for name, primary in BUILD_PROPERTIES.items():
        aliases = [value for key, value in properties.items() if _property_alias(name, key, primary)]
        if aliases:
            result["build." + name] = min(min(91, max(63, len(value.encode("utf-8")))) for value in aliases)
    return result


def _property_alias(name, key, primary):
    return (key == primary
            or name == "product" and key == "ro.build.product"
            or name in {"id", "incremental", "fingerprint"} and key.startswith("ro.") and key.endswith(primary[2:])
            or name in {"model", "device", "product"} and key.startswith("ro.product.")
            and key.endswith("." + primary.rsplit(".", 1)[-1]))


def observe_consumers(broker, manager, target, selected, deadline):
    """Read actual consumers, not staged desired-value files; absent views stay absent."""
    epoch = manager._userspace_runtime_epoch(deadline=deadline)
    if not epoch: raise ProfilePending("profile_consumer_epoch_unavailable")
    commands = {"props": ["getprop"], "cpuinfo": ["cat", "/proc/cpuinfo"],
                "meminfo": ["cat", "/proc/meminfo"], "battery": ["dumpsys", "battery"]}
    if any(path.startswith("input.") for path in selected): commands["input"] = ["dumpsys", "input"]
    sources = {}
    def source(path, label, filename, divisor=1):
        key = "f" + str(len(sources))
        sources[key] = (path, label, divisor)
        commands[key] = ["cat", filename]
    for path in selected:
        match = re.fullmatch(r"cpu\.cores\.([0-7])\.(minimumFrequencyKhz|maximumFrequencyKhz)", path)
        if match:
            kind = "min" if match[2].startswith("minimum") else "max"
            source(path, "sysfs", "/sys/devices/system/cpu/cpu%s/cpufreq/cpuinfo_%s_freq" % (match[1], kind))
        match = re.fullmatch(r"thermal\.(zone[0-9])\.temp", path)
        if match: source(path, "sysfs", "/sys/class/thermal/thermal_%s/temp" % match[1])
        if path == "usb.product":
            source(path, "usb_gadget", "/config/usb_gadget/g1/strings/0x409/product")
            source(path, "android_usb", "/sys/class/android_usb/android0/iProduct")
        battery_files = {"capacityMah": ("charge_full_design", 1000), "chargeFullDesignUah": ("charge_full_design", 1),
                         "chargeFullUah": ("charge_full", 1), "chargeCounterUah": ("charge_counter", 1)}
        if path.startswith("battery.") and path[8:] in battery_files:
            filename, divisor = battery_files[path[8:]]
            source(path, "sysfs", "/sys/class/power_supply/battery/" + filename, divisor)
    lines = ["# XENOID_PROFILE_CONSUMERS", "emit() { key=$1; shift; value=$(\"$@\" 2>/dev/null); code=$?; printf '%s:%s:' \"$key\" \"$code\"; printf '%s' \"$value\" | base64 | tr -d '\\r\\n'; printf '\\n'; }"]
    lines += ["emit " + shlex.quote(key) + " " + shlex.join(command) for key, command in commands.items()]
    raw = _execute(broker, manager, ["sh", "-c", "\n".join(lines)], deadline)
    data, errors = {}, {}
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3: raise ProfilePending("profile_consumer_response_invalid")
        key, code, encoded = parts
        if key not in commands or key in data or key in errors:
            raise ProfilePending("profile_consumer_response_invalid")
        if code != "0": errors[key] = "unavailable"; continue
        try: data[key] = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError): raise ProfilePending("profile_consumer_response_invalid")
    if set(data) | set(errors) != set(commands):
        raise ProfilePending("profile_consumer_response_incomplete")
    values = {path: {} for path in selected}
    props = _properties(data.get("props", ""))
    for name, prop in BUILD_PROPERTIES.items():
        path = "build." + name
        if path in values:
            for key, value in props.items():
                if _property_alias(name, key, prop):
                    values[path]["root_getprop" if key == prop else "getprop:" + key] = value
    if "build.id" in values and "ro.build.display.id" in props:
        values["build.id"]["root_display_id"] = props["ro.build.display.id"]
    cores = {}
    for block in re.split(r"\n\s*\n", data.get("cpuinfo", "")):
        fields = dict(re.findall(r"(?m)^([^:\n]+?)\s*:\s*([^\n]*)$", block))
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        if fields.get("processor", "").isdigit(): cores[int(fields["processor"])] = fields
    if "cpu.implementer" in values:
        for index, fields in cores.items():
            if "CPU implementer" in fields: values["cpu.implementer"]["procfs_cpu%d" % index] = fields["CPU implementer"]
    for index, fields in cores.items():
        for key, label in (("part", "CPU part"), ("model", "model name")):
            path = "cpu.cores.%d.%s" % (index, key)
            if path in values and label in fields: values[path]["procfs"] = fields[label]
    touch = list(re.finditer(r"^  Device -?\d+: " + re.escape(target["input"]["name"]) + r"\n(.*?)(?=^  Device |^  Configuration:|\Z)", data.get("input", ""), re.M | re.S))
    if len(touch) == 1:
        axes = re.search(r"Raw Touch Axes:\n(.*?)^      Calibration:", touch[0][1], re.M | re.S)
        for axis in ("x", "y"):
            match = re.search(r"^        " + axis.upper() + r": min=(-?\d+), max=(-?\d+),", axes[1], re.M) if axes else None
            if match:
                for key, number in zip(("minimum", "maximum"), match.groups()):
                    path = "input." + axis + "." + key
                    if path in values: values[path]["input_reader"] = int(number)
    mem = dict(re.findall(r"(?m)^(MemTotal|SwapTotal):\s*(\d+)\s+kB\s*$", data.get("meminfo", "")))
    for path, name, factor in (("memory.totalBytes", "MemTotal", 1024), ("memory.totalKiB", "MemTotal", 1), ("memory.swapBytes", "SwapTotal", 1024)):
        if path in values and name in mem: values[path]["procfs_meminfo"] = int(mem[name]) * factor
    battery = {key.strip(): value.strip() for key, value in re.findall(r"(?m)^\s*([^:\n]+):\s*([^\n]+)$", data.get("battery", ""))}
    for key in ("level", "voltage", "temperature", "status", "health", "scale"):
        path = "battery." + key
        if path in values and battery.get(key, "").lstrip("-").isdigit(): values[path]["dumpsys_battery"] = int(battery[key])
    if "battery.plugged" in values and any(key in battery for key in ("AC powered", "USB powered", "Wireless powered")):
        values["battery.plugged"]["dumpsys_battery"] = sum(number for key, number in (("AC powered", 1), ("USB powered", 2), ("Wireless powered", 4)) if battery.get(key) == "true")
    for key, (path, label, divisor) in sources.items():
        if key not in data: continue
        value = data[key].strip()
        if isinstance(_profile_value(target, path), int):
            if not value.lstrip("-").isdigit() or int(value) % divisor: continue
            value = int(value) // divisor
        values[path][label] = value
    display = _display(broker, manager, deadline)
    for path, value in (("display.width", display["size"][0]), ("display.height", display["size"][1]), ("display.densityDpi", display["density"])):
        if path in values: values[path]["window_manager"] = value
    if "display.defaultRefreshRateHz" in values:
        values["display.defaultRefreshRateHz"]["surfaceflinger"] = int(round(1000000000 / _display_period(broker, manager, deadline)))
    if manager._userspace_runtime_epoch(deadline=deadline) != epoch:
        raise ProfilePending("profile_consumer_epoch_changed")
    return {"schema": "dev.xenoid.profile-consumers/v1", "observed_at": _now(), "values": values, "errors": errors,
            "userspace_epoch": epoch,
            "scope": "root/shell consumers; app-process cached values and physical allocation are not inferred"}


def audit_consumers(before, after, target, selected):
    fields, effective = {}, []
    for path in selected:
        old, observed = before.get("values", {}).get(path, {}), after.get("values", {}).get(path, {})
        wanted = _profile_value(target, path)
        def matches(value):
            if path == "cpu.implementer" or path.endswith(".part") and path.startswith("cpu."):
                try: return int(str(value), 16) == int(str(wanted), 16)
                except ValueError: return False
            return value == wanted or str(value) == str(wanted)
        matched = [source for source, value in observed.items() if matches(value)]
        changed = [source for source in matched if source in old and observed[source] != old[source]]
        if not observed: status = "unobservable"
        elif len(matched) != len(observed): status = "partial" if matched else "unchanged" if observed == old else "mismatch"
        elif changed: status = "applied"
        else: status = "unchanged" if old else "verified_without_before"
        if changed: effective.append(path)
        fields[path] = {"selected": True, "requested": wanted, "before": old, "observed": observed,
                        "status": status, "changed_consumers": changed}
    return {"schema": "dev.xenoid.profile-observation/v1", "observed_at": after["observed_at"],
            "fields": fields, "effective_changes": effective, "scope": after["scope"],
            "counts": {status: sum(row["status"] == status for row in fields.values()) for status in
                       ("applied", "partial", "unchanged", "mismatch", "unobservable", "verified_without_before")}}


def _preflight_profile(policy, actual, previous, resource, capacities):
    """Reject known-impossible choices before committing native GEN.

    Placeholder widths are fixed by generate_profile; no random function or
    persisted operation key is consulted by this validation-only preview.
    """
    for name in BUILD_PROPERTIES:
        path = "build." + name
        row = policy["fields"].get(path, {})
        if row.get("enabled") is not True:
            continue
        capacity = capacities.get(path, min(91, max(63, len(str(actual["build"][name]).encode("utf-8")))))
        for candidate in row.get("values", []):
            materialized = candidate.replace("{token}", "0" * 16).replace("{digits}", "0" * 6)
            if len(materialized.encode("utf-8")) > capacity:
                raise ProfilePending("profile_property_capacity_exceeded")
    preview = generate_profile(actual, policy, "profile-feasibility-validation-only",
                               previous=previous, resource_config=resource, property_capacities=capacities)
    selected = set(selected_fields(previous, policy))
    if selected and not _fixed_display_only(policy, selected) and not (selected - {"display.peakRefreshRateHz"}) & set(preview["changed_fields"]):
        raise ProfilePending("profile_policy_has_no_feasible_change")
    return {"capacity_checked": True, "feasible": True, "validation_only": True}


def _fixed_display_only(policy, selected):
    random_display = policy.get("display_random")
    if random_display is not None:
        return (random_display.get("enabled") is True and len(display_random_candidates(random_display)) == 1
                and not set(selected) - set(DISPLAY_PRESET_FIELDS))
    presets = policy.get("display_presets") or {}
    return (presets.get("enabled") is True and len(presets.get("values", [])) == 1
            and not set(selected) - set(DISPLAY_PRESET_FIELDS))


def _fresh_contract(resource, actual, policy, previous=None):
    if resource.get("render_fps") is None:
        return
    from qa_fresh6a import validate_profile_contract, validate_policy_contract
    try:
        validate_profile_contract(actual)
        if previous is not None:
            validate_profile_contract(previous)
        validate_policy_contract(policy, actual)
    except ValueError as error:
        raise ProfilePending("fresh6a_profile_policy_incompatible") from error


def capture_plan(broker, manager, binding, deadline, *, config_path=CONFIG, resource_path=RESOURCE_CONFIG):
    """Pin settings and the device's current effective JSON before GEN starts."""
    if not Path(config_path).exists():
        return None  # Backwards-compatible workers with no profile policy.
    raw = _read_json(config_path)
    if not isinstance(raw, dict):
        raise ProfilePending("profile_policy_invalid")
    if raw.get("enabled") is False:
        return None
    actual = _effective(broker, manager, deadline)
    capacity_properties = _properties(_execute(broker, manager, ["getprop"], deadline))
    policy = validate_policy(raw, [actual])
    if not policy["enabled"]:
        return None
    display = _display(broker, manager, deadline)
    display["period_ns"] = _display_period(broker, manager, deadline)
    display["fps"] = int(round(1000000000 / display["period_ns"]))
    resource = _read_json(resource_path) if Path(resource_path).exists() else {}
    resource = {**resource, "physical_size": display["physical_size"], "physical_density": display["physical_density"]}
    previous = copy.deepcopy(actual)
    previous["display"].update(width=display["size"][0], height=display["size"][1], densityDpi=display["density"])
    previous["display"]["defaultRefreshRateHz"] = display["fps"]
    previous["display"]["peakRefreshRateHz"] = max(display["fps"], previous["display"]["peakRefreshRateHz"])
    for axis, value in (("x", display["size"][0]), ("y", display["size"][1])):
        previous["input"][axis] = {"minimum": 0, "maximum": value - 1}
    validate_profile(previous)
    _fresh_contract(resource, actual, policy, previous)
    capacities = _property_capacities(capacity_properties)
    preflight = _preflight_profile(policy, actual, previous, resource, capacities)
    return {"schema": PLAN_SCHEMA, "enabled": True, "binding": copy.deepcopy(binding), "policy": policy,
            "policy_sha256": profile_sha256(policy), "base_profile": actual, "base_sha256": profile_sha256(actual),
            "previous_profile": previous, "previous_sha256": profile_sha256(previous),
            "property_capacities": capacities, "preflight": preflight,
            "property_capacity_source": "inferred_from_live_property_utf8_lengths",
            "resource_config": resource, "display_before": display, "captured_at": _now()}


def _plan(receipt):
    plan = receipt.get("profile_plan")
    if plan is None:
        return None
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA or plan.get("enabled") is not True or plan.get("binding") != receipt.get("binding"):
        raise ProfilePending("profile_plan_invalid")
    for field, digest in (("base_profile", "base_sha256"), ("previous_profile", "previous_sha256"), ("policy", "policy_sha256")):
        if profile_sha256(plan.get(field)) != plan.get(digest):
            raise ProfilePending("profile_plan_digest_mismatch")
    validate_profile(plan["base_profile"]); validate_profile(plan["previous_profile"])
    validate_policy(plan["policy"], [plan["base_profile"], plan["previous_profile"]])
    _fresh_contract(plan["resource_config"], plan["base_profile"], plan["policy"], plan["previous_profile"])
    return plan


def _native_ids(manager, cli):
    identity = cli.DeviceIdentityStore(manager.context).load()
    if not identity or identity.get("pending") or identity.get("pendingStable") or not identity.get("active"):
        raise ProfilePending("profile_native_identity_unavailable")
    stable = identity["stable"]
    return {"android_id": stable["androidId"], "serial": stable["serial"], "imei": stable["imei"],
            "imeisv": stable["imeisv"], "boot_id": identity["active"]["bootId"]}


def _verify_core(broker, manager, cli, receipt, deadline):
    broker.verify_regeneration_native_completion(manager, cli, receipt, deadline)
    if broker.regeneration_binding(manager) != receipt["binding"]:
        raise ProfilePending("profile_container_binding_changed")
    target = receipt["native_journal"]["target"]
    commands = {"android_id": ["settings", "get", "secure", "android_id"], "serial": ["getprop", "ro.serialno"],
                "imei": ["getprop", "persist.xenoid.radio.imei"], "imeisv": ["getprop", "persist.xenoid.radio.imeisv"],
                "boot_id": ["cat", "/proc/sys/kernel/random/boot_id"],
                "drm": ["cat", "/data/local/tmp/xenoid-profile/drm_device_unique_id"],
                "fsid": ["cat", "/data/local/tmp/xenoid-profile/statfs_fsid"]}
    native = _native_ids(manager, cli)
    expected = {**native, "drm": target["drmDeviceUniqueId"], "fsid": target["statfsFsid"]}
    for key, command in commands.items():
        if _execute(broker, manager, command, deadline) != expected[key]:
            raise ProfilePending("profile_changed_native_" + key)
    return native


def _matches_profile(broker, manager, target, deadline):
    try: return profile_sha256(_effective(broker, manager, deadline)) == profile_sha256(target)
    except (ValueError, ProfilePending): return False


def _staged_fields(target):
    fields = {"cpu_count": len(target["cpu"]["cores"]), "cpu_implementer": target["cpu"]["implementer"]}
    for index, core in enumerate(target["cpu"]["cores"]):
        for key, value in core.items():
            if key in {"processor", "part", "model", "minimumFrequencyKhz", "maximumFrequencyKhz"}:
                fields["cpu_" + str(index) + "_" + key] = value
    for key in ("totalBytes", "totalKiB", "swapBytes"):
        fields["memory_" + key] = target["memory"][key]
    for key in ("capacityBytes", "blockDevice", "mountSource", "sectorSizeBytes", "sectorCount", "technology", "filesystem"):
        fields["storage_" + key] = target["storage"][key]
    for key, value in target["battery"].items(): fields["battery_" + key] = value
    for key in ("manufacturer", "product", "vendor_id", "product_id"):
        if key in target.get("usb", {}): fields["usb_" + key] = target["usb"][key]
    for zone, values in target.get("thermal", {}).items():
        if re.fullmatch(r"zone[0-9]", zone):
            for key in ("type", "temp"):
                if key in values: fields["thermal_" + zone + "_" + key] = values[key]
    display = target["display"]
    fields.update(display_width=display["width"], display_height=display["height"], display_density_dpi=display["densityDpi"],
                  display_physicalPpi=display["physicalPpi"], display_defaultRefreshRateHz=display["defaultRefreshRateHz"], display_peakRefreshRateHz=display["peakRefreshRateHz"],
                  input_width=display["width"], input_height=display["height"])
    for key in ("name", "busType", "vendorId", "productId", "version"):
        fields["input_" + key] = target["input"][key]
    for axis in ("x", "y", "pressure", "trackingId"):
        for key in ("minimum", "maximum"):
            fields["input_" + axis + "_" + key] = target["input"][axis][key]
    return {key: str(value) for key, value in fields.items()}


def _verify_staged(broker, manager, target, deadline):
    expected = _staged_fields(target)
    if any(not re.fullmatch(r"[A-Za-z0-9_]+", key) for key in expected):
        raise ProfilePending("profile_staged_field_invalid")
    script = "# XENOID_PROFILE_STAGES\nset -eu; for key in " + " ".join(sorted(expected)) + "; do printf '%s=' \"$key\"; base64 <\"/data/local/tmp/xenoid-profile/$key\" | tr -d '\\r\\n'; printf '\\n'; done"
    output = _execute(broker, manager, ["sh", "-c", script], deadline)
    observed = {}
    for line in output.splitlines():
        key, sep, encoded = line.partition("=")
        if not sep or key in observed: raise ProfilePending("profile_staged_readback_invalid")
        try:
            value = base64.b64decode(encoded, validate=True).decode()
            observed[key] = value[:-1] if value.endswith("\n") else value
        except (ValueError, UnicodeDecodeError): raise ProfilePending("profile_staged_readback_invalid")
    if observed != expected:
        raise ProfilePending("profile_staged_fields_mismatch")
    return len(expected)


def _verify_applied_fields(broker, manager, target, deadline, *, check_build=True):
    if not _matches_profile(broker, manager, target, deadline):
        raise ProfilePending("profile_effective_json_mismatch")
    properties = {"model": "ro.product.model", "fingerprint": "ro.build.fingerprint", "id": "ro.build.id",
                  "incremental": "ro.build.version.incremental", "release": "ro.build.version.release",
                  "sdk": "ro.build.version.sdk", "security_patch": "ro.build.version.security_patch",
                  "bootloader": "ro.bootloader"}
    if check_build:
        for name, prop in properties.items():
            if name in BUILD_PROPERTIES:
                continue  # Mutable reported properties receive per-consumer audit below.
            if _execute(broker, manager, ["getprop", prop], deadline) != str(target["build"][name]):
                raise ProfilePending("profile_build_readback_mismatch")
    display = _display(broker, manager, deadline)
    if display["size"] != [target["display"]["width"], target["display"]["height"]] or display["density"] != target["display"]["densityDpi"]:
        raise ProfilePending("profile_display_readback_mismatch")
    return _verify_staged(broker, manager, target, deadline)


def _matches_applied_fields(broker, manager, target, deadline):
    try:
        # The official fingerprint apply stages data first. Read-only Build
        # properties are published by the separate native hide/identity apply.
        _verify_applied_fields(broker, manager, target, deadline, check_build=False)
        return True
    except (ValueError, ProfilePending):
        return False


def _verify_readback(broker, manager, target, deadline):
    return _verify_applied_fields(broker, manager, target, deadline)


def _failed_action(value, *, allow_reported_property=False):
    if isinstance(value, dict):
        if value.get("required") is False:
            return False
        if allow_reported_property and str(value.get("field", "")).startswith("ro."):
            return False
        if ("ok" in value and value["ok"] is not True
                or any(type(value.get(key)) is int and value[key] != 0 for key in ("exit", "exitCode"))):
            return True
        return any(_failed_action(child, allow_reported_property=allow_reported_property) for child in value.values())
    if isinstance(value, list): return any(_failed_action(child, allow_reported_property=allow_reported_property) for child in value)
    return False


def _reported_property_failures(value):
    def command_failures(node):
        if isinstance(node, dict):
            own = int(any(type(node.get(key)) is int and node[key] != 0 for key in ("exit", "exitCode")))
            return own + sum(command_failures(child) for child in node.values())
        if isinstance(node, list): return sum(command_failures(child) for child in node)
        return 0
    found = []
    if isinstance(value, dict):
        failures = command_failures(value) if str(value.get("field", "")).startswith("ro.") else 0
        if str(value.get("field", "")).startswith("ro.") and ("ok" in value and value["ok"] is not True or failures):
            found.append({"property": value["field"], "status": "failed", "failed_command_count": failures})
        else:
            for child in value.values(): found.extend(_reported_property_failures(child))
    elif isinstance(value, list):
        for child in value: found.extend(_reported_property_failures(child))
    return found


def load_applied(instance_id, resource_tag=None, container_id=None, *, state_dir=STATE):
    if not isinstance(instance_id, str) or not _INSTANCE.fullmatch(instance_id):
        return None
    path = Path(state_dir) / (instance_id + ".json")
    try:
        value = _read_json(path)
        if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA or value.get("instance_id") != instance_id:
            return None
        if resource_tag is not None and value.get("resource_tag") != resource_tag or container_id is not None and value.get("container_id") != container_id:
            return None
        if profile_sha256(value.get("profile")) != value.get("profile_sha256"):
            return None
        if (not re.fullmatch(r"[0-9a-f]{64}", str(value.get("profile_sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("policy_sha256", "")))
                or type(value.get("policy_revision")) is not int or value["policy_revision"] < 1
                or not isinstance(value.get("operation_key"), str) or not 1 <= len(value["operation_key"]) <= 200
                or not isinstance(value.get("applied_at"), str)
                or datetime.fromisoformat(value["applied_at"]).tzinfo is None
                or not re.fullmatch(r"[0-9a-f]{12}", str(value.get("resource_tag", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("container_id", "")))):
            return None
        validate_profile(value["profile"])
        if "field_audit" in value:
            if (not isinstance(value["field_audit"], dict)
                    or value["field_audit"].get("schema") != "dev.xenoid.profile-observation/v1"
                    or value.get("audit_sha256") != profile_sha256(value["field_audit"])):
                return None
        return value
    except (OSError, ValueError, ProfilePending):
        return None


def status_for_binding(instance_id, resource_tag=None, container_id=None, *, state_dir=STATE):
    value = load_applied(instance_id, resource_tag, container_id, state_dir=state_dir)
    if value is None: return None
    result = {"state": "applied", "sha256": value["profile_sha256"], "policy_revision": value["policy_revision"],
              "policy_sha256": value["policy_sha256"], "operation_key": value["operation_key"], "updated_at": value["applied_at"],
              "application_status": value.get("application_status", "applied")}
    if isinstance(value.get("field_audit"), dict):
        result.update(audit_sha256=profile_sha256(value["field_audit"]), audit_counts=value["field_audit"].get("counts", {}))
    return result


def pending_for_binding(instance_id, resource_tag=None, container_id=None, *, state_dir=STATE):
    """Let background resource reconciliation defer to a recorded profile phase."""
    if not isinstance(instance_id, str) or not _INSTANCE.fullmatch(instance_id):
        return None
    try:
        value = _read_json(Path(state_dir) / (instance_id + ".pending.json"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema") != PENDING_SCHEMA or value.get("instance_id") != instance_id or not isinstance(value.get("operation_key"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("target_sha256", ""))):
        raise ProfilePending("pending_profile_marker_invalid")
    if resource_tag is not None and value.get("resource_tag") != resource_tag or container_id is not None and value.get("container_id") != container_id:
        return None
    return value


def _remove_target(path, expected_sha256):
    """Remove only this operation's verified scratch file, never its siblings."""
    try:
        if not path.exists(): return True
        if profile_sha256(_read_json(path)) != expected_sha256: return False
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
        try:
            path.parent.rmdir()
            parent = os.open(path.parent.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try: os.fsync(parent)
            finally: os.close(parent)
        except OSError: pass  # Another operation's target can share the folder.
        return True
    except (OSError, ValueError, ProfilePending):
        return False


def _scope_rebase_matches(receipt, key, old_sha, new_sha):
    transition = receipt.get("profile_scope_transition")
    return (isinstance(transition, dict)
            and transition.get("schema") == "dev.xenoid.profile-scope-transition/v1"
            and transition.get("reason") == "administrator_json_only_scope_change"
            and transition.get("operation_key") == key
            and transition.get("old_target_sha256") == old_sha
            and transition.get("new_target_sha256") == new_sha)


def _profile_value(profile, path):
    current = profile
    for part in path.split("."):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _meaningful_changes(plan, target):
    previous = plan["previous_profile"]
    fields = plan["policy"]["fields"]
    selected = selected_fields(previous, plan["policy"])
    # Peak metadata is not a second independent physical display cadence.
    # The default rate is verified separately through the actual HWC period.
    changed = [path for path in selected if path != "display.peakRefreshRateHz"
               and _profile_value(previous, path) != _profile_value(target, path)]
    return selected, changed


def rebase_pending_profile(broker, manager, cli, receipt, store, key, new_policy, deadline=None, *, reason):
    """Explicit administrative scope migration; never a general retry behavior.

    The caller must hold the native lock and have stopped the owning observer.
    It archives the abandoned profile phase while preserving the exact completed
    native GEN transaction and IDs. Host-file reconciliation is replayable from
    this durable intent when complete() resumes.
    """
    progress = receipt.get("profile_progress") or {}
    old_plan = receipt.get("profile_plan")
    if (reason != "administrator_json_only_scope_change" or receipt.get("status") != "pending"
            or not isinstance(old_plan, dict) or old_plan.get("schema") != PLAN_SCHEMA
            or old_plan.get("binding") != receipt.get("binding")
            or (receipt.get("native_journal") or {}).get("phase") != "committed"
            or not receipt.get("native_completion") or progress.get("completed")
            or (progress.get("reload") or {}).get("requested")
            or receipt.get("post_cache_completed") or receipt.get("post_google_progress")):
        raise ProfilePending("profile_scope_rebase_not_allowed")
    native = _verify_core(broker, manager, cli, receipt, deadline)
    base, previous = copy.deepcopy(old_plan["base_profile"]), copy.deepcopy(old_plan["previous_profile"])
    if (profile_sha256(base) != old_plan.get("base_sha256")
            or profile_sha256(previous) != old_plan.get("previous_sha256")):
        raise ProfilePending("profile_scope_rebase_baseline_changed")
    validate_profile(base); validate_profile(previous)
    transition = receipt.get("profile_scope_transition")
    if transition is not None:
        policy = validate_policy(new_policy, [base, previous])
        history = receipt.get("profile_scope_history")
        archived = history[-1] if isinstance(history, list) and history else None
        archived_progress = archived.get("profile_progress") if isinstance(archived, dict) else None
        if (not isinstance(transition, dict)
                or not _scope_rebase_matches(receipt, key, transition.get("old_target_sha256"), progress.get("target_sha256"))
                or profile_sha256(policy) != old_plan.get("policy_sha256")
                or profile_sha256(old_plan.get("policy")) != old_plan.get("policy_sha256")
                or not isinstance(archived_progress, dict)
                or archived.get("reason") != reason
                or profile_sha256(archived_progress.get("target_profile")) != transition.get("old_target_sha256")
                or profile_sha256(progress.get("target_profile")) != transition.get("new_target_sha256")):
            raise ProfilePending("profile_scope_rebase_already_recorded")
        # A lost response must not replace the predecessor hash while its file
        # and pending marker still need migration to this fixed successor.
        return {"old_target_sha256": transition["old_target_sha256"],
                "new_target_sha256": transition["new_target_sha256"],
                "native_ids_preserved": True, "native_transaction_preserved": True}
    # Fields outside the JSON runtime's scope return to their captured native
    # baseline, rather than retaining a partially staged experimental value.
    for descriptor in field_catalog(base):
        if descriptor["mutable"] or descriptor["native_managed"]:
            continue
        parts = descriptor["path"].split(".")
        source, destination = base, previous
        for part in parts[:-1]:
            source = source[int(part)] if isinstance(source, list) else source[part]
            destination = destination[int(part)] if isinstance(destination, list) else destination[part]
        item = source[int(parts[-1])] if isinstance(source, list) else source[parts[-1]]
        if isinstance(destination, list): destination[int(parts[-1])] = copy.deepcopy(item)
        else: destination[parts[-1]] = copy.deepcopy(item)
    policy = validate_policy(new_policy, [base, previous])
    generated = generate_profile(base, policy, key, previous=previous, native_ids=native,
                                 resource_config=old_plan.get("resource_config"))
    old_target = progress.get("target_profile")
    old_sha = progress.get("target_sha256")
    if not isinstance(old_target, dict) or profile_sha256(old_target) != old_sha:
        raise ProfilePending("profile_scope_rebase_target_invalid")
    new_plan = {**old_plan, "policy": policy, "policy_sha256": profile_sha256(policy),
                "previous_profile": previous, "previous_sha256": profile_sha256(previous)}
    new_plan.pop("bridge_sha256", None)
    receipt.setdefault("profile_scope_history", []).append({"reason": reason, "archived_at": _now(),
        "profile_plan": copy.deepcopy(old_plan), "profile_progress": copy.deepcopy(progress)})
    receipt["profile_plan"] = new_plan
    receipt["profile_progress"] = {"target_profile": generated["profile"], "target_sha256": generated["sha256"],
                                   "generated": generated, "created_at": _now()}
    receipt["profile_scope_transition"] = {"schema": "dev.xenoid.profile-scope-transition/v1",
        "reason": reason, "operation_key": key, "old_target_sha256": old_sha,
        "new_target_sha256": generated["sha256"], "authorized_at": _now()}
    store.save(key, receipt)
    return {"old_target_sha256": old_sha, "new_target_sha256": generated["sha256"],
            "native_ids_preserved": True, "native_transaction_preserved": True}


def complete(broker, manager, cli, receipt, store, key, deadline, *, state_dir=STATE):
    """Resume the pinned profile operation; all retries reuse the same JSON."""
    plan = _plan(receipt)
    if plan is None:
        return None
    _budget(broker, deadline)
    native = _verify_core(broker, manager, cli, receipt, deadline)
    progress = receipt.setdefault("profile_progress", {})
    if not isinstance(progress, dict): raise ProfilePending("profile_progress_invalid")
    if "target_profile" not in progress:
        generated = generate_profile(plan["base_profile"], plan["policy"], key,
                                     previous=plan["previous_profile"], native_ids=native,
                                     resource_config=plan["resource_config"], property_capacities=plan.get("property_capacities"))
        progress.update(target_profile=generated["profile"], target_sha256=generated["sha256"],
                        generated=generated, created_at=_now())
        store.save(key, receipt)  # A random choice is durable before any apply.
    target = validate_profile(progress["target_profile"])
    if profile_sha256(target) != progress.get("target_sha256") or target["ids"] != native:
        raise ProfilePending("profile_target_digest_or_identity_mismatch")
    selected, meaningful = _meaningful_changes(plan, target)
    progress["selected_profile_fields"] = selected
    progress["meaningful_fields"] = meaningful
    fixed_display = _fixed_display_only(plan["policy"], selected)
    if selected and not meaningful and not fixed_display:
        progress["no_effect"] = True
        store.save(key, receipt)
        raise ProfilePending("profile_no_effect")
    instance = receipt["binding"]["instance_id"]
    if not _INSTANCE.fullmatch(instance): raise ProfilePending("profile_instance_invalid")
    path = Path(state_dir) / "pending" / instance / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    if path.exists():
        file_sha = profile_sha256(_read_json(path))
        if file_sha != progress["target_sha256"]:
            if _scope_rebase_matches(receipt, key, file_sha, progress["target_sha256"]):
                _atomic_json(path, target)
            else:
                raise ProfilePending("profile_target_file_changed")
    else:
        _atomic_json(path, target)
    pending_path = Path(state_dir) / (instance + ".pending.json")
    pending = pending_for_binding(instance, receipt["binding"]["resource_tag"], receipt["binding"]["container_id"], state_dir=state_dir)
    if pending is not None and (pending["operation_key"] != key or pending["target_sha256"] != progress["target_sha256"]):
        if pending["operation_key"] == key and _scope_rebase_matches(receipt, key, pending["target_sha256"], progress["target_sha256"]):
            pending = None  # The durable administrative migration names this exact old target.
        else:
            raise ProfilePending("another_profile_operation_pending")
    if pending is None:
        _atomic_json(pending_path, {"schema": PENDING_SCHEMA, **receipt["binding"], "operation_key": key,
                                    "target_sha256": progress["target_sha256"], "created_at": progress["created_at"]})
    if "consumer_before" not in progress:
        published = load_applied(instance, receipt["binding"]["resource_tag"], receipt["binding"]["container_id"], state_dir=state_dir)
        legacy_completed = bool(progress.get("completed") and published
            and published["profile_sha256"] == progress["target_sha256"] and published["operation_key"] == key)
        progress["consumer_before"] = (observe_consumers(broker, manager, target, selected, deadline)
                                       if not progress.get("apply_started") else
                                       {"values": {}, "legacy_completed": legacy_completed,
                                        "scope": "Earlier profile apply has no proven before observation"})
        progress["consumer_before_sha256"] = profile_sha256(progress["consumer_before"])
        store.save(key, receipt)
    if profile_sha256(progress["consumer_before"]) != progress.get("consumer_before_sha256"):
        raise ProfilePending("profile_consumer_baseline_changed")
    if not progress.get("apply_verified") or not progress.get("hide_verified"):
        if not progress.get("apply_started") or not _matches_applied_fields(broker, manager, target, deadline):
            progress["apply_started"] = True
            store.save(key, receipt)
            captured = []
            old_runtime, old_print = cli.runtime, cli.print_json
            cli.runtime = lambda _args: manager
            cli.print_json = lambda value: captured.append(value)
            args = argparse.Namespace(context=manager.context, config=manager.cfg, lease=manager.lease,
                                      profile=path, instance_identity=True, keep_unique=False,
                                      generate_frida=False, _operation_lock_held=True)
            try:
                result = cli.cmd_device_apply(args)
            finally:
                cli.runtime, cli.print_json = old_runtime, old_print
            answer = captured[-1] if captured else {}
            progress["official_apply_result"] = answer
            store.save(key, receipt)
            if result or not isinstance(answer, dict) or answer.get("ok") is not True or answer.get("rotated") is not False or _failed_action(answer, allow_reported_property=True):
                raise ProfilePending("profile_official_apply_pending")
        _verify_applied_fields(broker, manager, target, deadline, check_build=False)
        if not progress.get("hide_verified"):
            # This is the same official /hide/apply stage that native GEN uses
            # after apply_fingerprint. It publishes --identity properties.
            progress["hide_started"] = True
            store.save(key, receipt)
            captured = []
            old_runtime, old_print = cli.runtime, cli.print_json
            cli.runtime = lambda _args: manager
            cli.print_json = lambda value: captured.append(value)
            args = argparse.Namespace(context=manager.context, config=manager.cfg, lease=manager.lease,
                                      policy=None, _operation_lock_held=True)
            try:
                code = cli.cmd_hide_apply(args)
            finally:
                cli.runtime, cli.print_json = old_runtime, old_print
            answer = captured[-1] if captured else {}
            if (code or not isinstance(answer, dict) or answer.get("ok") is not True
                    or answer.get("failures") or _failed_action(answer)
                    or not any(action.get("name") == "property-area" and action.get("ok") is True
                               for action in answer.get("actions", []) if isinstance(action, dict))):
                raise ProfilePending("profile_official_property_apply_pending")
            _verify_core(broker, manager, cli, receipt, deadline)
            progress["hide_verified"] = True
            store.save(key, receipt)
        _verify_core(broker, manager, cli, receipt, deadline)
        progress["apply_verified"] = True
        store.save(key, receipt)

    # The official reload refreshes native profile consumers while explicitly
    # preserving the completed GEN identities and the per-app SSAID store.
    reload = progress.get("reload")
    if reload is None:
        epoch = manager._userspace_runtime_epoch(deadline=deadline)
        if not epoch: raise ProfilePending("profile_userspace_epoch_unavailable")
        reload = {"before_epoch": epoch, "token": hashlib.sha256(("profile-reload:" + key).encode()).hexdigest()[:32], "requested": False}
        progress["reload"] = reload; store.save(key, receipt)
    if not reload.get("completed"):
        epoch = manager._userspace_runtime_epoch(deadline=deadline)
        if reload.get("requested") and not epoch:
            raise ProfilePending("profile_reload_waiting_epoch")
        observed = bool(epoch and epoch != reload["before_epoch"])
        if observed and manager.regeneration_restart_receipt(reload["token"]).get("ok") is not True:
            raise ProfilePending("profile_reload_receipt_missing")
        reload["requested"] = True
        store.save(key, receipt)
        remaining = 180 if deadline is None else max(.1, min(180, deadline - time.monotonic()))
        result = manager.soft_reboot(timeout=remaining, require_health=False, reset_ssaid=False,
                                     userspace_restart_observed=observed, restart_receipt=reload["token"])
        after = manager._userspace_runtime_epoch(deadline=deadline)
        if after and after != reload["before_epoch"] and manager.regeneration_restart_receipt(reload["token"]).get("ok") is True:
            reload["after_epoch"] = after
            store.save(key, receipt)
        if result.get("ok") is not True:
            raise ProfilePending("profile_reload_pending")
        if not after or after == reload["before_epoch"]:
            raise ProfilePending("profile_reload_not_observed")
        reload["completed"] = True; store.save(key, receipt)
    _verify_core(broker, manager, cli, receipt, deadline)
    fps = int(target["display"]["defaultRefreshRateHz"])
    timeout = 0 if deadline is None else max(1, int(deadline - time.monotonic()))
    if plan.get("resource_config", {}).get("render_fps") is not None:
        from qa_fresh6a import ensure_runtime
        ensure_runtime(broker, manager, deadline=deadline)
    else:
        broker.set_frame_rate(fps, timeout=timeout)
    period = _display_period(broker, manager, deadline)
    if abs(period - 1000000000 // fps) > 1:
        raise ProfilePending("profile_frame_rate_readback_mismatch")
    stage_count = _verify_readback(broker, manager, target, deadline)
    try:
        display_input = broker.verify_native_display_geometry(manager, target, deadline)
    except RuntimeError as error:
        raise ProfilePending("profile_native_display_geometry_pending") from error
    if (not isinstance(display_input, dict) or display_input.get("ok") is not True
            or display_input.get("events_injected") is not False
            or display_input.get("native_profile_sha256") != profile_sha256(target)):
        raise ProfilePending("profile_native_display_geometry_unverified")
    _verify_core(broker, manager, cli, receipt, deadline)
    audit_epoch = manager._userspace_runtime_epoch(deadline=deadline)
    if progress.get("completed") and progress.get("field_audit") and progress.get("audit_epoch") == audit_epoch:
        audit = progress["field_audit"]
    else:
        observed = observe_consumers(broker, manager, target, selected, deadline)
        audit = audit_consumers(progress["consumer_before"], observed, target, selected)
        audit["official_property_failures"] = _reported_property_failures(progress.get("official_apply_result", {}))
        audit["official_failure_counts"] = {"properties": len(audit["official_property_failures"]),
            "commands": sum(row["failed_command_count"] for row in audit["official_property_failures"])}
        for failure in audit["official_property_failures"]:
            for name, prop in BUILD_PROPERTIES.items():
                field_path = "build." + name
                if field_path in audit["fields"] and _property_alias(name, failure["property"], prop):
                    audit["fields"][field_path].setdefault("reported_failures", []).append(failure["property"])
                    if audit["fields"][field_path]["status"] == "applied": audit["fields"][field_path]["status"] = "partial"
        audit["counts"] = {status: sum(row["status"] == status for row in audit["fields"].values()) for status in audit["counts"]}
        progress["audit_epoch"] = audit_epoch
    effective = [path for path in audit["effective_changes"] if path in meaningful]
    progress["field_audit"] = audit
    audit["display_input"] = display_input
    progress["effective_changes_verified"] = effective
    legacy_observation = progress["consumer_before"].get("legacy_completed") is True
    audit["legacy_before_unavailable"] = legacy_observation
    audit["intentional_fixed_display"] = fixed_display
    store.save(key, receipt)
    if selected and not effective and not legacy_observation and not fixed_display:
        raise ProfilePending("profile_no_effect")
    _verify_core(broker, manager, cli, receipt, deadline)
    application_status = "partial" if (audit.get("official_property_failures")
        or any(audit["fields"][path]["status"] != "applied" for path in meaningful)) else "applied"
    progress.setdefault("completed_at", _now())
    _atomic_json(Path(state_dir) / (instance + ".json"), {
        "schema": STATE_SCHEMA, **receipt["binding"], "profile": target,
        "profile_sha256": progress["target_sha256"], "policy_revision": plan["policy"]["revision"],
        "policy_sha256": plan["policy_sha256"], "operation_key": key, "applied_at": progress["completed_at"],
        "field_audit": audit, "audit_sha256": profile_sha256(audit), "application_status": application_status})
    progress["completed"] = True; store.save(key, receipt)
    pending = pending_for_binding(instance, receipt["binding"]["resource_tag"], receipt["binding"]["container_id"], state_dir=state_dir)
    if pending is None or pending["operation_key"] != key or pending["target_sha256"] != progress["target_sha256"]:
        raise ProfilePending("pending_profile_marker_changed")
    pending_path.unlink()
    directory = os.open(pending_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory)
    finally: os.close(directory)
    progress["target_file_removed"] = _remove_target(path, progress["target_sha256"])
    store.save(key, receipt)
    manager.clear_regeneration_restart_receipt(reload["token"])
    return {"ok": True, "profile_sha256": progress["target_sha256"], "policy_revision": plan["policy"]["revision"],
            "changed_fields": progress["generated"]["changed_fields"],
            "unchanged_enabled_fields": progress["generated"]["unchanged_enabled_fields"],
            "official_apply": True, "native_ids_preserved": True, "input_ready": True,
            "profile_reload_observed": True, "staged_fields_verified": stage_count,
            "fps": fps, "period_ns": period, "temporary_target_removed": progress["target_file_removed"],
            "effective_changes_verified": effective, "field_audit": audit, "application_status": application_status,
            "display_input": display_input, "display_source": progress["generated"].get("display_source", "legacy"),
            "display_selection": progress["generated"].get("display_selection", progress["generated"].get("display_preset")),
            "legacy_observation_unavailable": legacy_observation,
            "randomization_skipped": "no_enabled_fields" if not selected else "fixed_display_only" if fixed_display and not meaningful else None}
