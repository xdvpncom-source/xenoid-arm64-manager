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
DISPLAY_PRESET_FIELDS = ("display.width", "display.height", "display.densityDpi",
                         "display.defaultRefreshRateHz", "display.peakRefreshRateHz",
                         "input.x.maximum", "input.y.maximum")
DISPLAY_PRESET_MANAGED = set(DISPLAY_PRESET_FIELDS) | {"input.x.minimum", "input.y.minimum"}
DISPLAY_RANDOM_MANAGED = DISPLAY_PRESET_MANAGED
# Android 13 WindowManagerShellCommand.runDisplayDensity rejects density < 72:
# https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r1/services/core/java/com/android/server/wm/WindowManagerShellCommand.java
DISPLAY_MIN_DPI = 72
# Android 13 DisplayContent.setForcedSize clamps dimensions to at least 200:
# https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r1/services/core/java/com/android/server/wm/DisplayContent.java
DISPLAY_MIN_SIZE = 200
DEFAULT_DISPLAY_RANDOM = {"enabled": True, "base_width": 200, "base_height": 356,
                          "min_width": 200, "max_width": 220, "width_step": 2,
                          "base_dpi": 72, "fps": 20}
DEFAULT_DISPLAY_PRESETS = [
    {"width": 200, "height": 356, "dpi": 72, "fps": 20},
    {"width": 204, "height": 362, "dpi": 72, "fps": 20},
    {"width": 208, "height": 370, "dpi": 74, "fps": 20},
    {"width": 212, "height": 376, "dpi": 76, "fps": 20},
    {"width": 216, "height": 384, "dpi": 76, "fps": 20},
]
MAX_PROFILE_BYTES = 256 * 1024  # Native stageEffectiveProfile contract.
_MISSING = object()
_NATIVE_IDS = {"android_id", "serial", "imei", "imeisv", "boot_id"}
_BUILD_KEYS = set("brand manufacturer model device product board platform hardware soc_manufacturer soc_model release sdk id incremental description fingerprint bootloader security_patch first_api_level sku tags type kernel_build_suffix abi abilist abilist32 abilist64 bionic_arch dalvik_isa_arm64 dalvik_isa_arm".split())
_BUILD_RUNTIME = set("brand manufacturer platform hardware soc_manufacturer soc_model release sdk first_api_level sku tags type bootloader security_patch abi abilist abilist32 abilist64 bionic_arch dalvik_isa_arm64 dalvik_isa_arm".split())
_BUILD_LINKED = ("brand", "product", "device", "release", "id", "incremental", "type", "tags", "description", "fingerprint")
_DISPLAY_KEYS = {"width", "height", "densityDpi", "physicalPpi", "defaultRefreshRateHz", "peakRefreshRateHz"}
_BATTERY_KEYS = set("level scale voltage temperature status plugged health present technology capacityMah minimumCapacityMah chargeFullDesignUah chargeFullUah chargeCounterUah".split())
_INPUT_KEYS = {"name", "busType", "vendorId", "productId", "version"}
_FINGERPRINT = re.compile(r"^(?P<brand>[^/\s]+)/(?P<product>[^/\s]+)/(?P<device>[^:\s]+):(?P<release>[^/\s]+)/(?P<id>[^/\s]+)/(?P<incremental>[^:\s]+):(?P<type>[^/\s]+)/(?P<tags>[^\s]+)$")
_TOKENS = re.compile(r"\{([^{}]+)\}")
# ARM/Qualcomm part mappings: Linux arch/arm64/include/asm/cputype.h and
# util-linux sys-utils/lscpu-arm.c. These are virtual reported 4+2+2 bundles.
_CPU_REPORTS = (
    ("0x41", (("0xd05", "Cortex-A55"), ("0xd0b", "Cortex-A76"), ("0xd44", "Cortex-X1"))),
    ("0x51", (("0x805", "Kryo-4XX-Silver"), ("0x804", "Kryo-4XX-Gold"), ("0x804", "Kryo-4XX-Gold"))),
)


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
        reason = "A different hardware/OS family requires a matching native image." if not mutable else "Reported JSON identity; per-surface observations show what actually changed. Fingerprint components remain linked."
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
        mutable, reason = True, "Reported RAM/swap through native JSON and /proc; physical allocation and sysinfo may differ. Bytes/KiB and alignment remain linked."
    elif path == "cpu.implementer" or re.fullmatch(r"cpu\.cores\.[0-7]\.(part|model)", path):
        mutable, reason = True, "Reported CPU identity through native JSON and /proc; physical CPU remains unchanged. Eight cores and 4+2+2 cluster agreement are required."
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


def field_catalog(base_profile=None, policy=None):
    if base_profile is None:
        paths = ["build." + k for k in sorted(_BUILD_KEYS)] + ["ids." + k for k in sorted(_NATIVE_IDS)] + ["display." + k for k in sorted(_DISPLAY_KEYS)]
        result = [_descriptor(path) for path in sorted(paths)]
    else:
        if not isinstance(base_profile, dict):
            raise ValueError("Base profile must be an object")
        result = [_descriptor(path, value) for path, value in _leaves(base_profile)]
    display_owner = next((key for key in ("display_random", "display_presets")
                          if isinstance(policy, dict) and key in policy), None)
    if display_owner:
        for row in result:
            if row["path"] in DISPLAY_PRESET_MANAGED:
                row.update(mutable=False, managed_by=display_owner,
                           reason="The atomic display configuration controls dimensions, DPI, FPS and derives official native input bounds; disabling it freezes this tuple.")
    return result


def selected_fields(profile, policy):
    if not policy.get("enabled", True): return []
    display_group = policy.get("display_random", policy.get("display_presets"))
    result = [row["path"] for row in field_catalog(profile)
              if row["mutable"] and policy.get("fields", {}).get(row["path"], {}).get("enabled") is True
              and (display_group is None or row["path"] not in DISPLAY_PRESET_MANAGED)]
    if display_group is not None and display_group.get("enabled") is True:
        result.extend(path for path in DISPLAY_PRESET_FIELDS if path not in result)
    return result


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
    def variant(value):
        # Equal-length alternatives fit the native short/long property slots.
        # Replace a six-digit run with the existing per-operation template;
        # other identifiers retain their punctuation and character classes.
        match = re.search(r"\d{6}", value)
        if match:
            return value[:match.start()] + "{digits}" + value[match.end():]
        for index in range(len(value)-1, -1, -1):
            char = value[index]
            if char.isdigit(): replacement = str((int(char)+1) % 10)
            elif "a" <= char <= "z": replacement = chr((ord(char)-97+1) % 26+97)
            elif "A" <= char <= "Z": replacement = chr((ord(char)-65+1) % 26+65)
            else: continue
            return value[:index] + replacement + value[index+1:]
        return value
    choices["build.model"] += ["Xenoid QA A", "Xenoid QA B"]
    def virtual_name(original, number):
        # Preserve fingerprint length and the native property's available room.
        prefix = "qa"[:max(0, len(original)-1)]
        return prefix + str(number).zfill(len(original)-len(prefix))
    choices["build.board"] += [virtual_name(build["board"], number) for number in (1, 2)]
    # Include both build-only and product/device variants so disabled product
    # checkboxes do not prevent an otherwise coherent build-only rotation.
    for identity_change in (0, 1, 2):
        candidate = dict(build, id=variant(build["id"]), incremental=variant(build["incremental"]))
        if identity_change:
            candidate.update(product=virtual_name(build["product"], identity_change), device=virtual_name(build["device"], identity_change))
        candidate["fingerprint"] = "{brand}/{product}/{device}:{release}/{id}/{incremental}:{type}/{tags}".format(**candidate)
        candidate["description"] = "{product}-{type} {release} {id} {incremental} {tags}".format(**candidate)
        for key in ("id", "incremental", "fingerprint", "description", "product", "device"):
            choices["build." + key] += [candidate[key]]
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
    for implementer, clusters in _CPU_REPORTS:
        choices["cpu.implementer"].append(implementer)
        for index in range(8):
            part, model = clusters[0 if index < 4 else 1 if index < 6 else 2]
            choices["cpu.cores.%d.part" % index].append(part)
            choices["cpu.cores.%d.model" % index].append(model)
    return {path: _unique(values) for path, values in choices.items()}


def default_policy(base_profiles=None, resource_config=None):
    profiles = base_profiles or []
    if isinstance(profiles, dict):
        profiles = [profiles]
    result = {"schema": SCHEMA, "enabled": True, "revision": 1, "fields": {},
              "display_random": copy.deepcopy(DEFAULT_DISPLAY_RANDOM)}
    for raw in profiles:
        profile = normalize_resources(validate_profile(raw), resource_config)
        choices = _default_choices(profile)
        for desc in field_catalog(profile):
            path = desc["path"]
            row = result["fields"].setdefault(path, {"enabled": True, "values": []})
            row["values"] = _unique(row["values"] + choices[path]) if desc["mutable"] else []
    return result


def migrate_policy(policy, base_profiles, resource_config=None):
    """Explicit new revision; retain every existing administrator choice."""
    result = validate_policy(copy.deepcopy(policy), base_profiles)
    defaults = default_policy(base_profiles, resource_config)
    for path, row in defaults["fields"].items():
        reported = (path in {"build." + name for name in ("model", "device", "product", "board", "id", "incremental", "fingerprint", "description")}
                    or path in {"memory.totalBytes", "memory.totalKiB", "memory.swapBytes", "cpu.implementer"}
                    or re.fullmatch(r"cpu\.cores\.[0-7]\.(part|model)", path))
        current = result["fields"].get(path)
        if reported and row["values"] and (current is None or current["enabled"] and not current["values"]):
            result["fields"][path] = copy.deepcopy(row)
    if "display_presets" not in result and "display_random" not in result:
        result["display_random"] = copy.deepcopy(defaults["display_random"])
    result["revision"] += 1
    return validate_policy(result, base_profiles)


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
        # New scalar choices must leave a nonzero native raw-axis span. Keep
        # validate_profile permissive enough to inspect historical documents.
        _number(value, path, DISPLAY_MIN_SIZE, 32767)
    if path == "display.densityDpi":
        _number(value, path, DISPLAY_MIN_DPI, 65535)
    if path in ("display.defaultRefreshRateHz", "display.peakRefreshRateHz"):
        _number(value, path, 1, 1000000000)
    if path == "display.physicalPpi" and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
        raise ValueError("Display values must be positive finite numbers: " + path)
    if path.startswith("memory."):
        minimum = 0 if path.endswith("swapBytes") else 1048576 if path.endswith("totalKiB") else 1073741824
        _number(value, path, minimum, 1 << 50)
        if value % (4 if path.endswith("totalKiB") else 4096 if path.endswith("totalBytes") else 1024):
            raise ValueError("Memory choice has invalid alignment: " + path)
    if re.fullmatch(r"cpu\.cores\.[0-7]\.(minimumFrequencyKhz|maximumFrequencyKhz)", path):
        _number(value, path, 1, 10000000)
    if path == "cpu.implementer" or re.fullmatch(r"cpu\.cores\.[0-7]\.part", path):
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9A-Fa-f]{1,8}", value):
            raise ValueError("Invalid native CPU identifier: " + path)
    if re.fullmatch(r"cpu\.cores\.[0-7]\.model", path):
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 64 or "\n" in value or "\r" in value:
            raise ValueError("CPU model must be a nonempty native label of at most 64 characters")
    if path.startswith("build.") and isinstance(value, str):
        if not value.strip() or "\n" in value or "\r" in value: raise ValueError("Build choice must be a nonempty single line: " + path)
        if path == "build.fingerprint":
            materialized = value.replace("{token}", "a" * 24).replace("{digits}", "1" * 12)
            if not _FINGERPRINT.fullmatch(materialized): raise ValueError("Invalid build fingerprint candidate")


def display_random_candidates(group):
    """Validate a bounded width range and derive complete native display tuples."""
    if not isinstance(group, dict) or set(group) != set(DEFAULT_DISPLAY_RANDOM):
        raise ValueError("Random display requires enabled, base dimensions, width bounds/step, base DPI and FPS")
    if type(group["enabled"]) is not bool:
        raise ValueError("Random display enabled must be boolean")
    for key in ("base_width", "base_height", "min_width", "max_width", "width_step", "base_dpi", "fps"):
        minimum = DISPLAY_MIN_SIZE if key in {"min_width", "max_width"} else 2 if key in {"base_width", "base_height"} else DISPLAY_MIN_DPI if key == "base_dpi" else 1
        maximum = 65535 if key == "base_dpi" else 1000000000 if key == "fps" else 32767
        if type(group[key]) is not int or not minimum <= group[key] <= maximum:
            raise ValueError("Random display " + key + " is outside the native integer range")
    if group["min_width"] > group["max_width"]:
        raise ValueError("Random display minimum width must not exceed maximum width")
    candidates = []
    for width in range(group["min_width"], group["max_width"] + 1, group["width_step"]):
        # Exact integer arithmetic: round to nearest 2 px / 1 DPI; ties upward.
        height = 2 * ((width * group["base_height"] + group["base_width"]) // (2 * group["base_width"]))
        dpi = max(DISPLAY_MIN_DPI, (2 * width * group["base_dpi"] + group["base_width"]) // (2 * group["base_width"]))
        if not DISPLAY_MIN_SIZE <= height <= 32767 or not DISPLAY_MIN_DPI <= dpi <= 65535:
            raise ValueError("Random display range derives dimensions or DPI outside the native range")
        candidates.append({"width": width, "height": height, "dpi": dpi, "fps": group["fps"]})
    return candidates


def _uniform_choice(seed, values):
    """An unbiased, replayable choice; do not consume the script's random stream."""
    count = len(values)
    limit = (1 << 256) - ((1 << 256) % count)
    counter = 0
    while True:
        draw = int.from_bytes(hashlib.sha256(seed + b"display-random" + str(counter).encode("ascii")).digest(), "big")
        if draw < limit:
            return values[draw % count]
        counter += 1


def validate_policy(value, base_profiles=None, previous=None):
    if not isinstance(value, dict) or set(value) - {"schema", "enabled", "revision", "fields", "display_presets", "display_random"}:
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
    if "display_random" in value:
        if "display_presets" in value:
            raise ValueError("Random display and display presets are mutually exclusive")
        display_random_candidates(value["display_random"])
        result["display_random"] = copy.deepcopy(value["display_random"])
    if "display_presets" in value:
        group = value["display_presets"]
        if (not isinstance(group, dict) or set(group) - {"enabled", "values"}
                or type(group.get("enabled")) is not bool or not isinstance(group.get("values"), list)):
            raise ValueError("Display presets require enabled and a values list")
        presets, seen = [], set()
        for preset in group["values"]:
            if not isinstance(preset, dict) or set(preset) != {"width", "height", "dpi", "fps"}:
                raise ValueError("Each display preset requires exactly width, height, dpi and fps")
            for key, upper in (("width", 32767), ("height", 32767), ("dpi", 65535), ("fps", 1000000000)):
                minimum = DISPLAY_MIN_SIZE if key in {"width", "height"} else DISPLAY_MIN_DPI if key == "dpi" else 1
                if type(preset[key]) is not int or not minimum <= preset[key] <= upper:
                    raise ValueError("Display preset " + key + " is outside the native integer range")
            identity = tuple(preset[key] for key in ("width", "height", "dpi", "fps"))
            if identity in seen: raise ValueError("Duplicate display preset")
            seen.add(identity); presets.append(copy.deepcopy(preset))
        if group["enabled"] and not presets: raise ValueError("Enabled display presets require at least one preset")
        result["display_presets"] = {"enabled": group["enabled"], "values": presets}
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


def generate_profile(base, policy, operation_key, previous=None, native_ids=None, resource_config=None, property_capacities=None):
    if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 4096:
        raise ValueError("A durable operation key is required")
    base = validate_profile(base)
    current = validate_profile(previous) if previous is not None else copy.deepcopy(base)
    policy = validate_policy(policy, [base, current])
    if policy["enabled"] and previous is None and "display_presets" not in policy and "display_random" not in policy:
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
    presets = policy.get("display_presets")
    random_display = policy.get("display_random")
    display_group = random_display if random_display is not None else presets
    chosen_preset = None

    def enabled(path):
        return (policy["enabled"] and catalog.get(path, {}).get("mutable", False)
                and fields.get(path, {}).get("enabled", False)
                and (display_group is None or path not in DISPLAY_PRESET_MANAGED))

    if display_group is not None and policy["enabled"] and display_group["enabled"]:
        current_preset = {"width": current["display"]["width"], "height": current["display"]["height"],
                          "dpi": current["display"]["densityDpi"], "fps": current["display"]["defaultRefreshRateHz"]}
        if random_display is not None:
            candidates = display_random_candidates(random_display)
            different = [item for item in candidates if (item["width"], item["height"]) != (current_preset["width"], current_preset["height"])]
            chosen_preset = _uniform_choice(seed, different or candidates)
        else:
            chosen_preset = min(presets["values"], key=lambda item: (
                (item["width"], item["height"]) == (current_preset["width"], current_preset["height"]),
                item == current_preset, hashlib.sha256(seed + b"display-preset" + canonical_bytes(item)).digest()))
        result["display"].update(width=chosen_preset["width"], height=chosen_preset["height"],
                                 densityDpi=chosen_preset["dpi"], defaultRefreshRateHz=chosen_preset["fps"],
                                 peakRefreshRateHz=chosen_preset["fps"])
        for axis, dimension in (("x", "width"), ("y", "height")):
            result["input"][axis] = {"minimum": 0, "maximum": chosen_preset[dimension] - 1}

    def values(path):
        old = _get(current, path)
        pool = fields.get(path, {}).get("values", []) if enabled(path) else []
        expanded = [item.replace("{token}", token).replace("{digits}", digits) if isinstance(item, str) else item for item in pool]
        candidates = _unique(expanded + ([] if old is _MISSING else [old]))
        return sorted(candidates, key=lambda item: (item == old, hashlib.sha256(seed + path.encode() + canonical_bytes(item)).digest()))

    def allowed(path, value):
        if path.startswith("build.") and isinstance(value, str):
            old = _get(current, path, "")
            capacity = (property_capacities or {}).get(path, min(91, max(63, len(str(old).encode("utf-8")))))
            if type(capacity) is not int or capacity < 0 or len(value.encode("utf-8")) > capacity:
                reasons[path] = "Candidate exceeds the observed native property slot capacity"
                return False
        return value == _get(current, path) or enabled(path) and value in values(path)

    def apply_group(proposals, paths, name):
        handled.update(paths)
        if display_group is not None and all(path in DISPLAY_PRESET_MANAGED for path in paths):
            return  # The preset/freeze is the sole owner of this tuple.
        valid = [proposal for proposal in proposals if all(allowed(path, value) for path, value in proposal.items())]
        if valid:
            proposal = max(valid, key=lambda item: (
                sum(enabled(path) and value != _get(current, path) for path, value in item.items()),
                hashlib.sha256(seed + canonical_bytes(item)).digest()))
            for path, value in proposal.items(): _set(result, path, value)
            return
        for path in paths:
            if enabled(path): reasons.setdefault(path, name + ": no candidate preserves disabled/linked fields")

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

    # The default reports are complete virtual CPU descriptions; they do not
    # change the host processor or claim a real device with this 4+2+2 topology.
    identity_paths = ["cpu.implementer"] + ["cpu.cores.%d.%s" % (i, key) for i in range(8) for key in ("part", "model")]
    reports = [(current["cpu"]["implementer"], tuple((current["cpu"]["cores"][i]["part"], current["cpu"]["cores"][i]["model"]) for i in (0, 4, 6))), *_CPU_REPORTS]
    proposals = []
    for implementer, clusters in reports:
        proposal = {"cpu.implementer": implementer}
        for index in range(8):
            part, model = clusters[0 if index < 4 else 1 if index < 6 else 2]
            proposal.update({"cpu.cores.%d.part" % index: part, "cpu.cores.%d.model" % index: model})
        proposals.append(proposal)
    # Explicit custom fields retain cluster agreement, including when the
    # implementer or part checkbox is off. These remain reported metadata.
    for implementer in values("cpu.implementer"):
        proposal = {"cpu.implementer": implementer}
        for leader, members in ((0, range(4)), (4, range(4, 6)), (6, range(6, 8))):
            for key in ("part", "model"):
                value = next(candidate for candidate in values("cpu.cores.%d.%s" % (leader, key))
                             if all(allowed("cpu.cores.%d.%s" % (index, key), candidate) for index in members))
                proposal.update({"cpu.cores.%d.%s" % (index, key): value for index in members})
        proposals.append(proposal)
    apply_group(proposals, identity_paths, "reported CPU identity bundle")
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
        pool = [value for value in values(path) if allowed(path, value)]
        if pool: _set(result, path, pool[0])
    result = validate_profile(result)
    changed = [path for path, value in _leaves(result) if value != _get(current, path)]
    unchanged = []
    for path, desc in catalog.items():
        if enabled(path) and _get(result, path) == _get(current, path):
            unchanged.append({"path": path, "reason": reasons.get(path, "No distinct compatible configured candidate")})
    return {"profile": result, "sha256": profile_sha256(result), "changed_fields": changed, "unchanged_enabled_fields": unchanged, "managed_fields": [path for path, desc in catalog.items() if desc["native_managed"]], "policy_revision": policy["revision"], "seed_sha256": seed.hex(), "qa_build_metadata": any(path.startswith("build.") for path in changed), "unique_profile": bool(changed), "enabled": policy["enabled"],
            "display_preset": copy.deepcopy(chosen_preset),
            "display_selection": copy.deepcopy(chosen_preset),
            "display_source": "legacy" if display_group is None else ("random" if random_display is not None else "presets") if policy["enabled"] and display_group["enabled"] else "frozen"}
