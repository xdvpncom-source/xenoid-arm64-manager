"""Deterministic, panel-owned native device-profile generation.

This module never touches a device.  A generated document is a full native
``dev.xenoid.fingerprint/v1`` profile for the official instance-identity apply
path.  The caller owns the durable operation receipt and supplies fresh native
IDs after regeneration.  A checkbox cannot freeze an ID owned by native GEN.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
from typing import Any, Mapping

SCHEMA = "dev.xenoid.profile-policy/v1"
PROFILE_SCHEMA = "dev.xenoid.fingerprint/v1"
MAX_PROFILE_BYTES = 256 * 1024  # Native stageEffectiveProfile contract.
_MISSING = object()
_NATIVE_IDS = {"android_id", "serial", "imei", "imeisv", "boot_id"}
_BUILD_KEYS = set("brand manufacturer model device product board platform hardware soc_manufacturer soc_model release sdk id incremental description fingerprint bootloader security_patch first_api_level sku tags type kernel_build_suffix abi abilist abilist32 abilist64 bionic_arch dalvik_isa_arm64 dalvik_isa_arm".split())
_BUILD_RUNTIME = set("brand manufacturer model device product board platform hardware soc_manufacturer soc_model release sdk first_api_level sku tags type bootloader security_patch abi abilist abilist32 abilist64 bionic_arch dalvik_isa_arm64 dalvik_isa_arm".split())
_BUILD_LINKED = ("brand", "product", "device", "release", "id", "incremental", "type", "tags", "description", "fingerprint")
_DISPLAY_KEYS = {"width", "height", "densityDpi", "physicalPpi", "defaultRefreshRateHz", "peakRefreshRateHz"}
_BATTERY_KEYS = set("level scale voltage temperature status plugged health present technology capacityMah minimumCapacityMah chargeFullDesignUah chargeFullUah chargeCounterUah".split())
_INPUT_KEYS = {"name", "busType", "vendorId", "productId", "version"}
_FINGERPRINT = re.compile(r"^(?P<brand>[^/\s]+)/(?P<product>[^/\s]+)/(?P<device>[^:\s]+):(?P<release>[^/\s]+)/(?P<id>[^/\s]+)/(?P<incremental>[^:\s]+):(?P<type>[^/\s]+)/(?P<tags>[^\s]+)$")
_TOKENS = re.compile(r"\{([^{}]+)\}")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def profile_sha256(profile):
    return hashlib.sha256(canonical_bytes(profile)).hexdigest()


def _get(value, path, default=_MISSING):
    try:
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def _set(value, path, item):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    if isinstance(value, list):
        value[int(parts[-1])] = copy.deepcopy(item)
    else:
        value[parts[-1]] = copy.deepcopy(item)


def _leaves(value, prefix=""):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _leaves(value[key], prefix + "." + key if prefix else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, prefix + "." + str(index))
    else:
        yield prefix, value


def _value_ok(value):
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= 8192 and "\0" not in value and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _unique(values):
    result, seen = [], set()
    for value in values:
        key = canonical_bytes(value)
        if key not in seen:
            result.append(copy.deepcopy(value)); seen.add(key)
    return result


def _descriptor(path, value=None):
    group = path.split(".", 1)[0]
    mutable, native, reason = False, False, "This value is descriptive or has no verified native profile setter."
    if path.startswith("ids.") and path.split(".", 1)[1] in _NATIVE_IDS or path == "usb.serial":
        native, reason = True, "Official regenerate owns this ID; the checkbox cannot freeze it."
    elif path.startswith("storage."):
        reason = "This native image requires its 128GB/UFS3.1/f2fs storage contract."
    elif path.startswith("build.") and path.split(".", 1)[1] in _BUILD_KEYS:
        key = path.split(".", 1)[1]
        mutable = key not in _BUILD_RUNTIME and key != "kernel_build_suffix"
        reason = "A different hardware/OS family requires a matching native image." if not mutable else "Build identity choices are applied as one coherent bundle; QA labels are not OEM certification."
        if key == "kernel_build_suffix": reason = "Kernel build suffix is compiled into the native runtime, not applied by this JSON setter."
    elif path.startswith("display.") and path.split(".", 1)[1] in _DISPLAY_KEYS:
        mutable, reason = path != "display.physicalPpi", "Dimensions and input coordinate limits stay synchronized. Refresh still uses the verified display adapter."
        if not mutable: reason = "The hardware PPI belongs to the fixed native display contract."
    elif path.startswith("display.modes.") or path.startswith("display.supportedRefreshRatesHz."):
        reason = "Compile-time hardware display modes; native JSON apply does not replace this catalog."
    elif path.startswith("battery.") and path.split(".", 1)[1] in _BATTERY_KEYS:
        mutable = path not in {"battery.scale", "battery.technology", "battery.health", "battery.present"}
        reason = "Battery level, charge and charging state must agree." if mutable else "Retain the declared, present healthy Li-ion battery; scale remains 100."
    elif re.fullmatch(r"thermal\.[^.]+\.(temp|type)", path):
        mutable = path.endswith(".temp")
        reason = "Virtual thermal profile temperature." if mutable else "Thermal zone types identify fixed hardware roles."
    elif path in {"memory.totalBytes", "memory.totalKiB", "memory.swapBytes"}:
        reason = "The native sysinfo contract fixes RAM/swap; a JSON-only override would disagree with the kernel view."
    elif path == "cpu.implementer" or re.fullmatch(r"cpu\.cores\.[0-7]\.(part|model)", path):
        reason = "The current eight-core hardware family is retained."
    elif re.fullmatch(r"cpu\.cores\.[0-7]\.(minimumFrequencyKhz|maximumFrequencyKhz)", path):
        mutable, reason = True, "Reported CPU frequency; all members of a native cluster must agree."
    elif path in {"input." + key for key in _INPUT_KEYS}:
        reason = "Keep the official native input-device contract."
    elif re.fullmatch(r"input\.(x|y)\.(minimum|maximum)", path):
        mutable = path.endswith("maximum")
        reason = "Derived with selected display dimensions; disabling it preserves the matching dimension."
    elif re.fullmatch(r"input\.(pressure|trackingId)\.(minimum|maximum)", path):
        reason = "Keep the verified official touch-axis contract."
    elif path in {"usb.manufacturer", "usb.product", "usb.vendor_id", "usb.product_id"}:
        mutable = path == "usb.product"
        reason = "Native USB product label." if mutable else "Retain the declared USB vendor/product hardware contract."
    return {"path": path, "label": path, "type": "boolean" if type(value) is bool else "number" if type(value) in (int, float) else "string", "group": group, "mutable": mutable, "native_managed": native, "reason": reason}


def field_catalog(base_profile=None):
    if base_profile is None:
        paths = ["build." + k for k in sorted(_BUILD_KEYS)] + ["ids." + k for k in sorted(_NATIVE_IDS)] + ["display." + k for k in sorted(_DISPLAY_KEYS)]
        return [_descriptor(path) for path in sorted(paths)]
    if not isinstance(base_profile, dict):
        raise ValueError("Base profile must be an object")
    return [_descriptor(path, value) for path, value in _leaves(base_profile)]


def _number(value, path, minimum, maximum):
    if type(value) is bool or not isinstance(value, (int, str)) or not re.fullmatch(r"-?\d+", str(value)):
        raise ValueError(path + " must be an integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(path + " is outside the native range")
    return number


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA:
        raise ValueError("Expected a native dev.xenoid.fingerprint/v1 profile")
    if len(canonical_bytes(profile)) > MAX_PROFILE_BYTES:
        raise ValueError("Profile exceeds the native 256 KiB contract")
    for key in ("build", "display", "input", "cpu", "memory", "storage", "battery"):
        if not isinstance(profile.get(key), dict):
            raise ValueError(key + " profile is required")
    hardware = profile.get("hardware_profile")
    if any(key in profile for key in ("network", "locale", "timezone")) or isinstance(hardware, dict) and any(key in hardware for key in ("mac", "wifi_mac")):
        raise ValueError("Network and location identity are owned by the official Location profile")
    for path, value in _leaves(profile):
        if not _value_ok(value):
            raise ValueError("Invalid JSON profile value at " + path)
    build = profile["build"]
    for key in _BUILD_KEYS - {"kernel_build_suffix"}:
        if key not in build or not isinstance(build[key], str) or (not build[key] and key not in {"abilist32", "dalvik_isa_arm"}):
            raise ValueError("Missing native build field: " + key)
    match = _FINGERPRINT.fullmatch(build["fingerprint"])
    if not match or any(build[k] != v for k, v in match.groupdict().items()):
        raise ValueError("Build fingerprint and component fields disagree")
    cpu = profile["cpu"]
    if not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", str(cpu.get("implementer", ""))) or not isinstance(cpu.get("cores"), list) or len(cpu["cores"]) != 8:
        raise ValueError("Native CPU contract requires eight cores and an implementer")
    for i, core in enumerate(cpu["cores"]):
        if core.get("processor") != i or not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", str(core.get("part", ""))) or not isinstance(core.get("model"), str) or len(core["model"]) > 64:
            raise ValueError("Invalid native CPU core " + str(i))
        low = _number(core.get("minimumFrequencyKhz"), "CPU minimum", 1, 10000000)
        _number(core.get("maximumFrequencyKhz"), "CPU maximum", low, 10000000)
        leader = 0 if i < 4 else 4 if i < 6 else 6
        if any(core[k] != cpu["cores"][leader][k] for k in ("part", "model", "minimumFrequencyKhz", "maximumFrequencyKhz")):
            raise ValueError("CPU cluster members disagree")
    mem = profile["memory"]
    total = _number(mem.get("totalBytes"), "memory.totalBytes", 4096, 1 << 50)
    kib = _number(mem.get("totalKiB"), "memory.totalKiB", 4, 1 << 40)
    swap = _number(mem.get("swapBytes"), "memory.swapBytes", 0, 1 << 50)
    if total != kib * 1024 or kib % 4 or swap % 1024:
        raise ValueError("Memory byte/KiB/swap values disagree")
    storage = {"variant": "128GB", "capacityBytes": 128000000000, "blockDevice": "sda", "mountSource": "/dev/block/platform/14700000.ufs/by-name/userdata", "sectorSizeBytes": 512, "sectorCount": 250000000, "technology": "UFS 3.1", "filesystem": "f2fs", "removable": False, "sparse": True}
    if any(profile["storage"].get(k) != v for k, v in storage.items()):
        raise ValueError("Storage differs from this native image contract")
    battery = profile["battery"]
    level = _number(battery.get("level"), "battery.level", 0, 100)
    scale = _number(battery.get("scale"), "battery.scale", 100, 100)
    _number(battery.get("voltage"), "battery.voltage", 1000, 6000)
    _number(battery.get("temperature"), "battery.temperature", -500, 1000)
    status = _number(battery.get("status"), "battery.status", 1, 5)
    plugged = _number(battery.get("plugged"), "battery.plugged", 0, 4)
    if plugged == 3 or plugged == 0 and status == 2 or plugged != 0 and status == 3 or status == 5 and level != scale:
        raise ValueError("Battery charging state disagrees")
    _number(battery.get("health"), "battery.health", 1, 7)
    _number(battery.get("present"), "battery.present", 0, 1)
    if battery.get("technology") != "Li-ion":
        raise ValueError("Native battery technology is Li-ion")
    capacity = _number(battery.get("capacityMah"), "battery.capacityMah", 1, 100000)
    _number(battery.get("minimumCapacityMah"), "battery.minimumCapacityMah", 1, capacity)
    design = _number(battery.get("chargeFullDesignUah"), "battery.chargeFullDesignUah", 1, 100000000)
    full = _number(battery.get("chargeFullUah"), "battery.chargeFullUah", 1, design)
    counter = _number(battery.get("chargeCounterUah"), "battery.chargeCounterUah", 0, full)
    if design != capacity * 1000 or counter != full * level // scale:
        raise ValueError("Battery charge and capacity values disagree")
    width = _number(profile["display"].get("width"), "display.width", 1, 32767)
    height = _number(profile["display"].get("height"), "display.height", 1, 32767)
    _number(profile["display"].get("densityDpi"), "display.densityDpi", 1, 65535)
    rates = [_number(profile["display"].get(k, 0), "display." + k, 1, 1000000000) for k in ("defaultRefreshRateHz", "peakRefreshRateHz")]
    if rates[0] > rates[1]:
        raise ValueError("Display refresh rates disagree")
    for axis, maximum in (("x", width - 1), ("y", height - 1)):
        if profile["input"].get(axis) != {"minimum": 0, "maximum": maximum}:
            raise ValueError("Display and official input " + axis + " axis disagree")
    return copy.deepcopy(profile)


def normalize_resources(profile, resource_config=None):
    result = copy.deepcopy(profile)
    if not resource_config or not resource_config.get("enabled", True):
        return result
    cfg = resource_config
    if cfg.get("scale", "minimum") is not None:
        physical = cfg.get("physical_size")
        if physical is None and "width" in cfg and "height" in cfg:
            width, height = int(cfg["width"]), int(cfg["height"])
        elif physical is not None:
            pw, ph = map(int, physical)
            scale = cfg.get("scale", "minimum")
            if scale == "minimum":
                side = int(cfg.get("min_side", 200)); factor = max(side / pw, side / ph)
                floor = max(2, (side + 1) // 2 * 2)
            else:
                factor, floor = float(scale), 2
            width, height = (max(floor, int(round(x * factor / 2)) * 2) for x in (pw, ph))
        else:
            raise ValueError("Resource normalization needs the device's actual physical_size or explicit width/height")
        result["display"].update(width=width, height=height, densityDpi=int(cfg.get("dpi", result["display"]["densityDpi"])))
        for axis, size in (("x", width), ("y", height)):
            result["input"][axis] = {"minimum": 0, "maximum": size - 1}
    fps = cfg.get("fps")
    if fps is not None and fps > 0:
        result["display"]["defaultRefreshRateHz"] = fps
        result["display"]["peakRefreshRateHz"] = fps
    return result


def _default_choices(profile):
    choices = {d["path"]: [_get(profile, d["path"])] for d in field_catalog(profile)}
    build = profile["build"]
    for suffix in ("q{token}", "q{digits}"):
        candidate = dict(build, id=build["id"] + ".QA{digits}", incremental=build["incremental"] + "." + suffix)
        candidate["fingerprint"] = "{brand}/{product}/{device}:{release}/{id}/{incremental}:{type}/{tags}".format(**candidate)
        candidate["description"] = "{product}-{type} {release} {id} {incremental} {tags}".format(**candidate)
        for key in ("id", "incremental", "fingerprint", "description"):
            choices["build." + key].append(candidate[key])
    display = profile["display"]
    width, height = int(display["width"]), int(display["height"])
    next_width = width + 2
    next_height = max(2, int(round((height / width) * next_width / 2)) * 2)
    choices["display.width"] += [next_width]; choices["display.height"] += [next_height]
    choices["input.x.maximum"] += [next_width - 1]; choices["input.y.maximum"] += [next_height - 1]
    choices["display.densityDpi"] += [int(display["densityDpi"]) + 2]
    choices["display.physicalPpi"] += [int(display["physicalPpi"]) + 1]
    choices["display.defaultRefreshRateHz"] += [int(display["defaultRefreshRateHz"]) + 1]
    choices["display.peakRefreshRateHz"] += [max(int(display["defaultRefreshRateHz"]) + 1, int(display["peakRefreshRateHz"]) + 1)]
    mem = profile["memory"]; extra = 4 * 1024 * 1024
    choices["memory.totalBytes"] += [int(mem["totalBytes"]) + extra]
    choices["memory.totalKiB"] += [int(mem["totalKiB"]) + extra // 1024]
    choices["memory.swapBytes"] += [int(mem["swapBytes"]) + extra]
    battery = profile["battery"]; caps = _unique([int(battery["capacityMah"]), int(battery["capacityMah"]) + 1])
    levels = _unique([int(battery["level"]), 67, 79, 91]); fulls = _unique([int(battery["chargeFullUah"])] + [n * 1000 for n in caps])
    variants = {"level": levels, "voltage": [3850, 3975, 4100], "temperature": [285, 310, 335], "status": [2, 3], "plugged": [0, 1], "capacityMah": caps, "minimumCapacityMah": [int(battery["minimumCapacityMah"]), max(1, int(battery["minimumCapacityMah"]) - 1)], "chargeFullDesignUah": [n * 1000 for n in caps], "chargeFullUah": fulls, "chargeCounterUah": [full * level // 100 for full in fulls for level in levels]}
    for key, values in variants.items():
        choices["battery." + key] += values
    for path, value in list(choices.items()):
        if re.fullmatch(r"thermal\.[^.]+\.temp", path):
            choices[path] += [int(value[0]) + 1000]
        elif re.fullmatch(r"cpu\.cores\.[0-7]\.(minimumFrequencyKhz|maximumFrequencyKhz)", path):
            choices[path] += [int(value[0]) + 1000]
        elif path == "usb.product":
            choices[path] += [str(value[0]) + " QA {digits}"]
    return {path: _unique(values) for path, values in choices.items()}


def default_policy(base_profiles=None, resource_config=None):
    profiles = base_profiles or []
    if isinstance(profiles, dict):
        profiles = [profiles]
    result = {"schema": SCHEMA, "enabled": True, "revision": 1, "fields": {}}
    for raw in profiles:
        profile = normalize_resources(validate_profile(raw), resource_config)
        choices = _default_choices(profile)
        for desc in field_catalog(profile):
            path = desc["path"]
            row = result["fields"].setdefault(path, {"enabled": True, "values": []})
            row["values"] = _unique(row["values"] + choices[path]) if desc["mutable"] else []
    return result


def _validate_choice(path, value, sample):
    if sample is not _MISSING:
        valid_type = (type(value) is bool if type(sample) is bool else type(value) is int if type(sample) is int else type(value) in (int, float) if type(sample) is float else isinstance(value, str) if isinstance(sample, str) else value is None)
        if not valid_type:
            raise ValueError("Profile choice type differs from native field: " + path)
    battery_ranges = {"level": (0, 100), "voltage": (1000, 6000), "temperature": (-500, 1000), "status": (1, 5), "plugged": (0, 4), "health": (1, 7), "present": (0, 1), "capacityMah": (1, 100000), "minimumCapacityMah": (1, 100000), "chargeFullDesignUah": (1, 100000000), "chargeFullUah": (1, 100000000), "chargeCounterUah": (0, 100000000)}
    if path.startswith("battery.") and path.split(".", 1)[1] in battery_ranges:
        lo, hi = battery_ranges[path.split(".", 1)[1]]
        _number(value, path, lo, hi)
        if path == "battery.plugged" and value == 3:
            raise ValueError("battery.plugged=3 is not a native valid source")
    if path in ("display.width", "display.height"):
        _number(value, path, 1, 32767)
    if path == "display.densityDpi":
        _number(value, path, 1, 65535)
    if path in ("display.defaultRefreshRateHz", "display.peakRefreshRateHz"):
        _number(value, path, 1, 1000000000)
    if path == "display.physicalPpi" and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
        raise ValueError("Display values must be positive finite numbers: " + path)
    if path.startswith("memory."):
        _number(value, path, 0 if path.endswith("swapBytes") else 4, 1 << 50)
        if value % (4 if path.endswith("totalKiB") else 1024):
            raise ValueError("Memory choice has invalid alignment: " + path)
    if re.fullmatch(r"cpu\.cores\.[0-7]\.(minimumFrequencyKhz|maximumFrequencyKhz)", path):
        _number(value, path, 1, 10000000)
    if path.startswith("build.") and isinstance(value, str):
        if not value.strip(): raise ValueError("Build choice cannot be empty: " + path)
        if path == "build.fingerprint":
            materialized = value.replace("{token}", "a" * 24).replace("{digits}", "1" * 12)
            if not _FINGERPRINT.fullmatch(materialized): raise ValueError("Invalid build fingerprint candidate")


def validate_policy(value, base_profiles=None, previous=None):
    if not isinstance(value, dict) or set(value) - {"schema", "enabled", "revision", "fields"}:
        raise ValueError("Unknown device profile policy field")
    if value.get("schema", SCHEMA) != SCHEMA or type(value.get("enabled", True)) is not bool:
        raise ValueError("Invalid device profile policy schema/enabled")
    revision = value.get("revision", (previous or {}).get("revision", 1))
    if type(revision) is not int or revision < 1:
        raise ValueError("Profile revision must be positive")
    fields = value.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("Profile policy fields must be an object")
    profiles = [base_profiles] if isinstance(base_profiles, dict) else base_profiles or []
    known = {}
    for profile in profiles:
        for desc in field_catalog(profile):
            known[desc["path"]] = desc
    result = {"schema": SCHEMA, "enabled": value.get("enabled", True), "revision": revision, "fields": {}}
    for path, row in fields.items():
        if not isinstance(path, str) or not path or len(path) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", path):
            raise ValueError("Invalid profile field path")
        if known and path not in known:
            raise ValueError("Unknown native profile path: " + path)
        desc = known.get(path, _descriptor(path))
        if not isinstance(row, dict) or set(row) - {"enabled", "values"} or type(row.get("enabled", True)) is not bool or not isinstance(row.get("values", []), list):
            raise ValueError("Invalid profile field policy: " + path)
        values = row.get("values", [])
        if len(values) > 128 or any(not _value_ok(item) for item in values):
            raise ValueError("Profile choices must be finite JSON scalar values: " + path)
        for item in values:
            if isinstance(item, str) and any(token not in {"token", "digits"} for token in _TOKENS.findall(item)):
                raise ValueError("Only {token} and {digits} templates are supported: " + path)
            if desc["mutable"]:
                sample = next((_get(profile, path) for profile in profiles if _get(profile, path) is not _MISSING), _MISSING)
                _validate_choice(path, item, sample)
        if not desc["mutable"]:
            if row.get("enabled", True) is not True:
                raise ValueError("Native-managed/fixed fields cannot be disabled: " + path)
            if values:
                raise ValueError("Native-managed/fixed field choices cannot override the native contract: " + path)
        result["fields"][path] = {"enabled": row.get("enabled", True), "values": _unique(values)}
    if len(canonical_bytes(result)) > MAX_PROFILE_BYTES:
        raise ValueError("Profile policy exceeds the native document budget")
    return result


def generate_profile(base, policy, operation_key, previous=None, native_ids=None, resource_config=None):
    if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 4096:
        raise ValueError("A durable operation key is required")
    base = validate_profile(base)
    current = validate_profile(previous) if previous is not None else copy.deepcopy(base)
    policy = validate_policy(policy, [base, current])
    if policy["enabled"] and previous is None:
        current = normalize_resources(current, resource_config)
    result = copy.deepcopy(current)
    if native_ids is not None:
        if not isinstance(native_ids, dict) or set(native_ids) != _NATIVE_IDS:
            raise ValueError("Supply exactly the five current native identity fields")
        result["ids"] = copy.deepcopy(native_ids)
        if isinstance(result.get("usb"), dict) and "serial" in result["usb"]:
            result["usb"]["serial"] = native_ids["serial"]
    seed = hashlib.sha256(b"dev.xenoid.profile-generation/v1\n" + operation_key.encode() + b"\n" + canonical_bytes(policy) + b"\n" + canonical_bytes(base)).digest()
    # Compact tokens retain a 64-bit per-operation fingerprint component while
    # keeping ordinary Pixel build fingerprints below Android's legacy limit.
    token = seed.hex()[:16]; digits = "%06d" % (int.from_bytes(seed[:8], "big") % 1000000)
    fields = policy["fields"]; catalog = {d["path"]: d for d in field_catalog(current)}
    handled = set(); reasons = {}

    def enabled(path):
        return policy["enabled"] and catalog.get(path, {}).get("mutable", False) and fields.get(path, {}).get("enabled", False)

    def values(path):
        old = _get(current, path)
        pool = fields.get(path, {}).get("values", []) if enabled(path) else []
        expanded = [item.replace("{token}", token).replace("{digits}", digits) if isinstance(item, str) else item for item in pool]
        candidates = _unique(expanded + ([] if old is _MISSING else [old]))
        return sorted(candidates, key=lambda item: (item == old, hashlib.sha256(seed + path.encode() + canonical_bytes(item)).digest()))

    def allowed(path, value):
        return value == _get(current, path) or enabled(path) and value in values(path)

    def apply_group(proposals, paths, name):
        handled.update(paths)
        for proposal in proposals:
            if all(allowed(path, value) for path, value in proposal.items()):
                for path, value in proposal.items(): _set(result, path, value)
                return
        for path in paths:
            if enabled(path): reasons[path] = name + ": no candidate preserves disabled/linked fields"

    # Parse one complete fingerprint; never randomly combine incompatible pieces.
    paths = ["build." + k for k in _BUILD_LINKED]
    proposals = []
    for fingerprint in values("build.fingerprint"):
        match = _FINGERPRINT.fullmatch(str(fingerprint))
        if not match: continue
        parts = match.groupdict()
        proposal = {"build." + k: v for k, v in parts.items()}
        proposal["build.fingerprint"] = fingerprint
        proposal["build.description"] = "{product}-{type} {release} {id} {incremental} {tags}".format(**parts)
        if fingerprint == current["build"]["fingerprint"]:
            proposal["build.description"] = current["build"]["description"]
        proposals.append(proposal)
    apply_group(proposals, paths, "coherent build")

    # Dimensions and native touch coordinates are one atomic choice.
    paths = ["display.width", "display.height", "input.x.maximum", "input.y.maximum"]
    width_values, height_values = values(paths[0]), values(paths[1])
    proposals = []
    for width, height in itertools.product(width_values, height_values):
        if type(width) is not int or type(height) is not int or min(width, height) <= 0: continue
        ratio = current["display"]["height"] / current["display"]["width"]
        if abs(height / width - ratio) > 2 / min(width, height): continue
        proposals.append({paths[0]: width, paths[1]: height, paths[2]: width - 1, paths[3]: height - 1})
    apply_group(proposals, paths, "display/input coordinates")

    rate_paths = ["display.defaultRefreshRateHz", "display.peakRefreshRateHz"]
    apply_group(({rate_paths[0]: default, rate_paths[1]: peak}
                 for default, peak in itertools.product(values(rate_paths[0]), values(rate_paths[1]))
                 if type(default) is int and type(peak) is int and 0 < default <= peak),
                rate_paths, "display refresh rates")

    paths = ["memory.totalBytes", "memory.totalKiB"]
    apply_group(({paths[0]: value, paths[1]: value // 1024} for value in values(paths[0]) if type(value) is int and value > 0 and value % 4096 == 0), paths, "memory bytes/KiB")

    for members in (range(4), range(4, 6), range(6, 8)):
        low_paths = ["cpu.cores." + str(i) + ".minimumFrequencyKhz" for i in members]
        high_paths = ["cpu.cores." + str(i) + ".maximumFrequencyKhz" for i in members]
        proposals = []
        for low, high in itertools.product(values(low_paths[0]), values(high_paths[0])):
            if type(low) is not int or type(high) is not int or low > high: continue
            proposals.append({**{p: low for p in low_paths}, **{p: high for p in high_paths}})
        apply_group(proposals, low_paths + high_paths, "CPU cluster")

    # Charge counter and design capacity must agree with the selected battery.
    paths = ["battery." + k for k in ("level", "status", "plugged", "capacityMah", "minimumCapacityMah", "chargeFullDesignUah", "chargeFullUah", "chargeCounterUah")]
    proposals = []
    iterables = [values("battery." + key) for key in ("level", "status", "plugged", "capacityMah", "minimumCapacityMah", "chargeFullUah")]
    for level, status, plugged, capacity, minimum, full in itertools.islice(itertools.product(*iterables), 8192):
        if any(type(v) is not int for v in (level, status, plugged, capacity, minimum, full)): continue
        if not 0 <= level <= 100 or plugged == 3 or plugged == 0 and status == 2 or plugged != 0 and status == 3 or status == 5 and level != 100 or minimum > capacity or full > capacity * 1000: continue
        proposals.append(dict(zip(paths, (level, status, plugged, capacity, minimum, capacity * 1000, full, full * level // 100))))
    apply_group(proposals, paths, "battery charge/state")

    for path, desc in catalog.items():
        if path in handled or not enabled(path): continue
        pool = values(path)
        if pool: _set(result, path, pool[0])
    result = validate_profile(result)
    changed = [path for path, value in _leaves(result) if value != _get(current, path)]
    unchanged = []
    for path, desc in catalog.items():
        if enabled(path) and _get(result, path) == _get(current, path):
            unchanged.append({"path": path, "reason": reasons.get(path, "No distinct compatible configured candidate")})
    return {"profile": result, "sha256": profile_sha256(result), "changed_fields": changed, "unchanged_enabled_fields": unchanged, "managed_fields": [path for path, desc in catalog.items() if desc["native_managed"]], "policy_revision": policy["revision"], "seed_sha256": seed.hex(), "qa_build_metadata": True, "unique_profile": bool(changed), "enabled": policy["enabled"]}
