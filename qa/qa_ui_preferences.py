"""Pinned Android user preferences, separate from native Location and identity.

The caller holds the existing native instance lock and persists capture()'s
return value as ``post_preferences_plan`` before starting GEN.  No selection is
made by complete(), including after a transport failure or a process restart.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import tempfile
import time

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 hosts: Android validates the zone before writes.
    ZoneInfo = None
    ZoneInfoNotFoundError = ValueError

STATE = Path("/var/lib/xenoid-fleet/ui-preferences")
SCHEMA = "dev.xenoid.ui-preferences/v1"
SNAPSHOT_SCHEMA = "dev.xenoid.ui-preferences-snapshot/v1"
PLAN_SCHEMA = "dev.xenoid.ui-preferences-plan/v1"
BASELINE_SCHEMA = "dev.xenoid.ui-preferences-baseline/v1"
NATIVE_BASELINE_SCHEMA = "dev.xenoid.native-location-baseline/v1"
HELPER = "dev.xenoid.daemon.UserUiPreferences"
_IID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_TAG = re.compile(r"[0-9a-f]{12}\Z")
_CID = re.compile(r"[0-9a-f]{64}\Z")
_LOCALE = re.compile(r"([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?((?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*)\Z")
_ZONE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_+-]*/)+[A-Za-z][A-Za-z0-9_+-]*\Z")
_SYSTEM = {"system_locales", "font_scale", "time_12_24", "date_format"}
_GLOBAL = {"auto_time_zone", "xenoid_qa_ui_preferences_v1"}
_FIELDS = ("locales", "timezone", "font_scale", "night_mode", "time_12_24", "date_format")
_CLASSPATH_ENV = {"BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH", "SYSTEMSERVERCLASSPATH", "STANDALONE_SYSTEMSERVER_JARS"}
_OVERLAY_NOTICES = {
    "boot_id", "random_sysctls", "cpuinfo", "version", "cmdline", "meminfo", "memory_proc_details",
    "cpu_proc_stats", "kernel_proc_misc", "kernel_hardening_sysctls", "proc_identity_tables", "mount_namespace",
    "cpu_sysfs", "storage_proc", "block_sysfs", "kallsyms_tracing", "virtualization_texts", "kernel_device_tables",
    "rtc_sysfs", "devicetree", "dmi_hypervisor", "build_props", "extra_build_props", "power_supply", "thermal_zones",
    "framebuffer", "backlight_leds", "usb_identity", "input_devices", "network_identity",
}
_HELPER_ERRORS = {"ui_preferences_invalid", "ui_preferences_root_required", "ui_preferences_setting_failed",
                  "ui_preferences_readback_mismatch", "invalid_user_preferences", "ui_preferences_operation_failed",
                  "ui_preferences_settings_timeout", "ui_preferences_settings_output_limit",
                  "ui_preferences_settings_output_ambiguous", "ui_preferences_settings_command_failed"}


def _region(name, locale, zone, clock="24", date="dd/MM/yyyy"):
    return {"name": name, "locales": [locale], "timezone": zone,
            "time_12_24": clock, "date_format": date}


# US presentation defaults. Explicit stored legacy pools remain valid; native
# carrier alignment is requested separately by the editor's native_location.
US_TIMEZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")
US_LOCALES = ("en-US", "es-US")
DEFAULT_REGIONS = [
    _region("United States — Eastern", "en-US", "America/New_York", "12", "MM/dd/yyyy"),
    _region("United States — Central", "en-US", "America/Chicago", "12", "MM/dd/yyyy"),
    _region("United States — Mountain", "en-US", "America/Denver", "12", "MM/dd/yyyy"),
    _region("United States — Pacific", "en-US", "America/Los_Angeles", "12", "MM/dd/yyyy"),
]


class PreferencesPending(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _locale(value):
    match = _LOCALE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Invalid BCP47 locale")
    language, script, region, variants = match.groups()
    return "-".join(part for part in (language.lower(), script.title() if script else None,
                                      region.upper() if region else None) if part) + variants.lower()


def _locales(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError("Use one to eight ordered locales")
    result = [_locale(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("Duplicate locale")
    return result


def _zone(value):
    if not isinstance(value, str) or len(value) > 100 or not (value == "UTC" or _ZONE.fullmatch(value)):
        raise ValueError("Invalid IANA timezone")
    if ZoneInfo is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone") from None
    return value


def _font(value, upper=2):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
        raise ValueError("Font scale must be finite and between zero and two")
    return float(value)


def _region_value(value):
    required = {"locales", "timezone", "time_12_24", "date_format"}
    if not isinstance(value, dict) or set(value) - required - {"name"} or not required <= set(value):
        raise ValueError("Region requires locales, timezone, time_12_24 and date_format")
    if "name" in value and (not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 80):
        raise ValueError("Invalid region name")
    if value["time_12_24"] not in ("12", "24"):
        raise ValueError("Clock format must be '12' or '24'")
    date = value["date_format"]
    if (not isinstance(date, str) or not 1 <= len(date) <= 32
            or not re.fullmatch(r"[yMd./ -]+", date) or not all(part in date for part in ("y", "M", "d"))):
        raise ValueError("Use a numeric year/month/day date format")
    result = {"locales": _locales(value["locales"]), "timezone": _zone(value["timezone"]),
              "time_12_24": value["time_12_24"], "date_format": date}
    if "name" in value:
        result["name"] = value["name"]
    return result


def normalize_plan(config):
    """Validate editor configuration without reading or changing a device."""
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError("UI_PREFERENCES must be an object")
    if set(config) - {"enabled", "regions", "font_scales", "night_modes", "native_location"}:
        raise ValueError("Unknown UI_PREFERENCES field")
    if type(config.get("enabled", True)) is not bool:
        raise ValueError("UI_PREFERENCES enabled must be boolean")
    if not config.get("enabled", True):
        return None
    regions = config.get("regions", DEFAULT_REGIONS)
    fonts = config.get("font_scales", [0.85, 1.0, 1.15])
    modes = config.get("night_modes", [1, 2])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 256:
        raise ValueError("Use one to 256 region choices")
    regions = [_region_value(value) for value in regions]
    if not isinstance(fonts, list) or not 1 <= len(fonts) <= 32:
        raise ValueError("Use one to 32 font choices")
    fonts = [_font(value) for value in fonts]
    if not isinstance(modes, list) or not modes or any(type(mode) is not int or mode not in (1, 2) for mode in modes):
        raise ValueError("Night modes are 1 (light) and 2 (dark)")
    def distinct(values):
        seen, result = set(), []
        for value in values:
            key = _bytes(value)
            if key not in seen:
                seen.add(key); result.append(value)
        return result
    # Names are labels, not distinct choices with the same Android settings.
    regions = distinct([{key: value for key, value in row.items() if key != "name"} for row in regions])
    result = {"enabled": True, "regions": regions, "font_scales": distinct(fonts), "night_modes": distinct(modes)}
    if "native_location" in config:
        native = config["native_location"]
        if not isinstance(native, dict) or set(native) != {"country", "randomize_carrier"}:
            raise ValueError("Native location requires country and randomize_carrier")
        if not isinstance(native["country"], str) or not re.fullmatch(r"[A-Z]{2}", native["country"]):
            raise ValueError("Native location country must be an uppercase ISO2 code")
        if type(native["randomize_carrier"]) is not bool:
            raise ValueError("Native randomize_carrier must be boolean")
        if native["country"] != "US" and "regions" not in config:
            raise ValueError("A non-US native location requires explicit region choices")
        if native["country"] == "US" and any(
                row["timezone"] not in US_TIMEZONES or any(locale not in US_LOCALES for locale in row["locales"])
                for row in regions):
            raise ValueError("Native US location requires matching US locales and timezones")
        result["native_location"] = copy.deepcopy(native)
    return result


def _binding(broker, manager, expected=None):
    value = broker.regeneration_binding(manager)
    if (not isinstance(value, dict) or not _IID.fullmatch(str(value.get("instance_id", "")))
            or not _TAG.fullmatch(str(value.get("resource_tag", "")))
            or not _CID.fullmatch(str(value.get("container_id", "")))):
        raise PreferencesPending("ui_preferences_binding_invalid")
    if expected is not None and value != expected:
        raise PreferencesPending("ui_preferences_binding_changed")
    return copy.deepcopy(value)


def _budget(broker, deadline):
    if broker.stopping.is_set():
        raise PreferencesPending("script_stopping")
    if deadline is not None and time.monotonic() >= deadline:
        raise PreferencesPending("ui_preferences_timeout")


def _execute(broker, manager, args, deadline):
    _budget(broker, deadline)
    result = broker.native_display_exec(manager, args, deadline=deadline, check=False)
    text = result.stdout.decode("utf-8", errors="strict").strip()
    if result.returncode:
        raise PreferencesPending("ui_preferences_helper_failed")
    return text


def _android_environment(broker, manager, deadline):
    """Copy only Android runtime variables from the live, pinned system_server."""
    command = ["pidof", "system_server"]
    pid = _execute(broker, manager, command, deadline)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        raise PreferencesPending("ui_preferences_android_environment_unavailable")
    _budget(broker, deadline)
    result = broker.native_display_exec(manager, ["cat", "/proc/" + pid + "/environ"], deadline=deadline, check=False)
    if result.returncode or not isinstance(result.stdout, bytes) or len(result.stdout) > 256 * 1024:
        raise PreferencesPending("ui_preferences_android_environment_unavailable")
    environment = {}
    for item in result.stdout.split(b"\0"):
        key, separator, raw = item.partition(b"=")
        try:
            name = key.decode("ascii")
        except UnicodeError:
            continue
        if name not in _CLASSPATH_ENV and not re.fullmatch(r"ANDROID_[A-Z0-9_]+", name):
            continue
        if not separator or name in environment or len(raw) > 64 * 1024:
            raise PreferencesPending("ui_preferences_android_environment_invalid")
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise PreferencesPending("ui_preferences_android_environment_invalid") from None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise PreferencesPending("ui_preferences_android_environment_invalid")
        environment[name] = value
    if (any(not environment.get(name) for name in ("ANDROID_ROOT", "ANDROID_DATA", "BOOTCLASSPATH"))
            or _execute(broker, manager, command, deadline) != pid):
        raise PreferencesPending("ui_preferences_android_environment_unavailable")
    return [name + "=" + environment[name] for name in sorted(environment)]


def _helper_reply(raw):
    """Accept one final JSON response, with only known successful native notices."""
    if not isinstance(raw, bytes) or len(raw) > 128 * 1024:
        raise PreferencesPending("ui_preferences_helper_response_invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        start = text.find("{")
        if start < 0:
            raise ValueError()
        if text[:start].strip() and text[start - 1] not in "\r\n":
            raise ValueError()
        notices = []
        for line in text[:start].splitlines():
            line = line.strip()
            if not line:
                continue
            name, separator, status = line.partition("=")
            if not separator or name not in _OVERLAY_NOTICES or status != "ok" or name in notices:
                raise ValueError()
            notices.append(name)
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        def bad_constant(_value):
            raise ValueError()
        decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=bad_constant)
        value, end = decoder.raw_decode(text, start)
        if text[end:].strip() or not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise PreferencesPending("ui_preferences_helper_response_invalid") from None


def _helper(broker, manager, action, payload, deadline):
    paths = _execute(broker, manager, ["pm", "path", "dev.xenoid.daemon"], deadline).splitlines()
    if (len(paths) != 1 or not paths[0].startswith("package:/")
            or not paths[0].endswith(".apk") or any(ch.isspace() for ch in paths[0])):
        raise PreferencesPending("ui_preferences_daemon_apk_unavailable")
    environment = _android_environment(broker, manager, deadline)
    args = ["env", "-i", "PATH=/system/bin:/system/xbin", *environment, "CLASSPATH=" + paths[0][8:],
            "/system/bin/app_process", "/system/bin", HELPER, action]
    if payload is not None:
        args.append(base64.b64encode(_bytes(payload)).decode("ascii"))
    _budget(broker, deadline)
    execution = broker.native_display_exec(manager, args, deadline=deadline, check=False)
    result = _helper_reply(execution.stdout)
    if result.get("ok") is not True:
        code = result.get("error")
        error = PreferencesPending(code if code in _HELPER_ERRORS else "ui_preferences_helper_rejected")
        error.rollback_attempted = result.get("rollback_attempted") is True
        error.rollback_succeeded = result.get("rollback_succeeded") is True
        raise error
    if execution.returncode:
        raise PreferencesPending("ui_preferences_helper_failed")
    return _snapshot(result.get("snapshot"))


def _snapshot(value):
    try:
        if (not isinstance(value, dict) or value.get("schema") != SNAPSHOT_SCHEMA
                or type(value.get("user_id")) is not int or value["user_id"] != 0):
            raise ValueError()
        fields, settings, props = value["values"], value["settings"], value["properties"]
        if set(fields) != {"locales", "timezone", "font_scale", "night_mode"}:
            raise ValueError()
        if fields["locales"] != _locales(fields["locales"]) or fields["timezone"] != _zone(fields["timezone"]):
            raise ValueError()
        _font(fields["font_scale"], upper=10)
        if type(fields["night_mode"]) is not int or fields["night_mode"] not in (0, 1, 2, 3):
            raise ValueError()
        if set(settings) != {"system", "global"} or set(settings["system"]) != _SYSTEM or set(settings["global"]) != _GLOBAL:
            raise ValueError()
        if any(raw is not None and not isinstance(raw, str) for group in settings.values() for raw in group.values()):
            raise ValueError()
        if set(props) != {"locale", "timezone"} or any(not isinstance(raw, str) for raw in props.values()):
            raise ValueError()
        return copy.deepcopy(value)
    except (KeyError, TypeError, ValueError):
        raise PreferencesPending("ui_preferences_snapshot_invalid") from None


def _observed_fields(snapshot):
    return {**snapshot["values"], "time_12_24": snapshot["settings"]["system"]["time_12_24"],
            "date_format": snapshot["settings"]["system"]["date_format"]}


def _matches(snapshot, target):
    actual = _observed_fields(snapshot)
    try:
        marker = json.loads(snapshot["settings"]["global"]["xenoid_qa_ui_preferences_v1"])
    except (ValueError, TypeError):
        return False
    return (all(actual[key] == target[key] for key in _FIELDS if key != "font_scale")
            and abs(actual["font_scale"] - target["font_scale"]) <= 0.00001
            and snapshot["settings"]["global"]["auto_time_zone"] == "0"
            and snapshot["properties"]["timezone"] == target["timezone"]
            and snapshot["properties"]["locale"] == target["locales"][0]
            and marker == target)


def _directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PreferencesPending("ui_preferences_state_directory_invalid")
    return path


def _sync_dir(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
        raise PreferencesPending("ui_preferences_state_file_invalid")
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise PreferencesPending("ui_preferences_state_file_invalid") from None


def _atomic(path, value):
    path = Path(path)
    _directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".ui-preferences-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_bytes(value) + b"\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _publish_once(path, value, validate):
    _directory(path.parent)
    if path.exists() or path.is_symlink():
        old = _read(path)
        validate(old)
        return old
    descriptor, temporary = tempfile.mkstemp(prefix=".ui-baseline-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_bytes(value) + b"\n"); stream.flush(); os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and cannot replace the first
            # baseline. A crash never exposes a half-written final document.
            os.link(temporary, str(path))
        except FileExistsError:
            old = _read(path)
            validate(old)
            return old
        _sync_dir(path.parent)
        return value
    finally:
        try:
            os.unlink(temporary)
            _sync_dir(path.parent)
        except FileNotFoundError:
            pass


def _baseline(path, binding, snapshot):
    value = {"schema": BASELINE_SCHEMA, "binding": binding, "snapshot": snapshot,
             "snapshot_sha256": _digest(snapshot), "captured_at": _now()}
    return _publish_once(path, value, lambda saved: _validate_baseline(saved, binding))


def _validate_baseline(value, binding):
    if (not isinstance(value, dict) or value.get("schema") != BASELINE_SCHEMA
            or value.get("binding") != binding or _digest(value.get("snapshot")) != value.get("snapshot_sha256")):
        raise PreferencesPending("ui_preferences_baseline_binding_or_digest_changed")
    return _snapshot(value["snapshot"])


def _validate_native_baseline(value, binding):
    country = value.get("country") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema") != NATIVE_BASELINE_SCHEMA
            or value.get("binding") != binding or not isinstance(country, str)
            or not re.fullmatch(r"[A-Z]{2}", country)
            or value.get("country_sha256") != _digest({"binding": binding, "country": country})):
        raise PreferencesPending("ui_preferences_native_baseline_binding_or_digest_changed")
    return country


def _capture_native_baseline(broker, manager, binding, state_dir):
    path = Path(state_dir) / (binding["instance_id"] + ".native-location-baseline.json")
    if path.exists() or path.is_symlink():
        return _validate_native_baseline(_read(path), binding)
    reader = getattr(broker, "native_location_baseline", None)
    if not callable(reader):
        raise PreferencesPending("ui_preferences_native_baseline_unavailable")
    country = reader(manager)
    if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
        raise PreferencesPending("ui_preferences_native_baseline_unavailable")
    _binding(broker, manager, binding)
    value = {"schema": NATIVE_BASELINE_SCHEMA, "binding": binding, "country": country,
             "country_sha256": _digest({"binding": binding, "country": country}), "captured_at": _now()}
    saved = _publish_once(path, value, lambda old: _validate_native_baseline(old, binding))
    return _validate_native_baseline(saved, binding)


def read_baseline(broker, manager, binding=None, deadline=None, *, state_dir=STATE):
    """Read the original UI snapshot and optional original native country.

    The native country is a separate, write-once attachment. This never
    replaces an older UI snapshot or invents a country for a legacy baseline.
    """
    _budget(broker, deadline)
    binding = _binding(broker, manager, binding)
    path = Path(state_dir) / (binding["instance_id"] + ".baseline.json")
    if not path.exists() and not path.is_symlink():
        raise PreferencesPending("ui_preferences_baseline_missing")
    saved = _read(path)
    snapshot = _validate_baseline(saved, binding)
    native_path = Path(state_dir) / (binding["instance_id"] + ".native-location-baseline.json")
    country = None
    if native_path.exists() or native_path.is_symlink():
        country = _validate_native_baseline(_read(native_path), binding)
    _binding(broker, manager, binding)
    return {"snapshot": snapshot, "native_country": country}


def _select(config, snapshot, key, binding, selection_version=2):
    if type(selection_version) is not int or selection_version not in (1, 2):
        raise PreferencesPending("ui_preferences_selection_version_invalid")
    prior = _observed_fields(snapshot)
    choices = [{**region, "font_scale": font, "night_mode": mode}
               for region, font, mode in itertools.product(config["regions"], config["font_scales"], config["night_modes"])]
    if selection_version == 1:
        eligible = [value for value in choices if value != prior]
    else:
        # Android stores fontScale as float32. Its observed 0.850000024 is
        # the same user setting as a configured 0.85, not a fresh choice.
        eligible = [value for value in choices if not (
            all(value[field] == prior[field] for field in _FIELDS if field != "font_scale")
            and abs(value["font_scale"] - prior["font_scale"]) <= 0.00001)]
    if eligible:
        choices = eligible
    seed_input = {"domain": "xenoid.ui-preferences/v1", "operation_key": key,
                  "binding": binding, "config": config, "previous": prior}
    if selection_version == 2:
        seed_input["selection_version"] = 2
    seed = _digest(seed_input)
    target = copy.deepcopy(random.Random(int(seed, 16)).choice(choices))
    return {"schema": SCHEMA, **target, "operation_key": key}


def capture(broker, manager, binding, config, deadline, operation_key, *, state_dir=STATE):
    config = normalize_plan(config)
    if config is None:
        return None
    if not isinstance(operation_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation_key):
        raise ValueError("Invalid UI preferences operation key")
    binding = _binding(broker, manager, binding)
    before = _helper(broker, manager, "read", None, deadline)
    _binding(broker, manager, binding)
    baseline = Path(state_dir) / (binding["instance_id"] + ".baseline.json")
    _baseline(baseline, binding, before)
    if "native_location" in config:
        _capture_native_baseline(broker, manager, binding, state_dir)
    target = _select(config, before, operation_key, binding)
    return {"schema": PLAN_SCHEMA, "binding": binding, "operation_key": operation_key,
            "independent_sim_region": "native_location" not in config, "selection_version": 2,
            "config": config, "config_sha256": _digest(config),
            "before_snapshot": before, "before_sha256": _digest(before),
            "target": target, "target_sha256": _digest(target), "captured_at": _now()}


def _plan(receipt, key):
    plan = receipt.get("post_preferences_plan")
    if plan is None:
        return None
    if (not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA or plan.get("binding") != receipt.get("binding")
            or plan.get("operation_key") != key or plan.get("target", {}).get("operation_key") != key):
        raise PreferencesPending("ui_preferences_plan_invalid")
    for field, digest in (("config", "config_sha256"), ("before_snapshot", "before_sha256"), ("target", "target_sha256")):
        if _digest(plan.get(field)) != plan.get(digest):
            raise PreferencesPending("ui_preferences_plan_digest_changed")
    config = normalize_plan(plan["config"])
    before = _snapshot(plan["before_snapshot"])
    if config is None or plan.get("independent_sim_region") is not ("native_location" not in config):
        raise PreferencesPending("ui_preferences_plan_invalid")
    if _select(config, before, key, plan["binding"], plan.get("selection_version", 1)) != plan["target"]:
        raise PreferencesPending("ui_preferences_target_changed")
    return plan


def native_location_args(plan):
    """Derive native CLI arguments from the exact persisted UI selection.

    No native change is implied for older plans without native_location. The
    caller passes these arguments before journal reservation; existing native
    transactions must retain the country/carrier target they already pinned.
    """
    if plan is None:
        return {}
    if not isinstance(plan, dict):
        raise PreferencesPending("ui_preferences_plan_invalid")
    checked = _plan({"binding": plan.get("binding"), "post_preferences_plan": plan}, plan.get("operation_key"))
    native = checked["config"].get("native_location")
    if native is None:
        return {}
    target = checked["target"]
    return {"location_country": native["country"], "randomize_carrier": native["randomize_carrier"],
            "location_timezone": target["timezone"], "location_locales": copy.deepcopy(target["locales"])}


def _rollback(broker, manager, plan, progress, store, key, receipt, deadline):
    progress.update(status="rollback_pending", rollback_required=True)
    store.save(key, receipt)
    _binding(broker, manager, plan["binding"])
    _helper(broker, manager, "restore", plan["before_snapshot"], deadline)
    actual = _helper(broker, manager, "read", None, deadline)
    if actual != plan["before_snapshot"]:
        raise PreferencesPending("ui_preferences_rollback_readback_mismatch")
    _binding(broker, manager, plan["binding"])
    progress.update(status="rolled_back", rollback_required=False, rollback_verified_at=_now())
    store.save(key, receipt)


def complete(broker, manager, receipt, store, key, deadline, *, state_dir=STATE):
    plan = _plan(receipt, key)
    if plan is None:
        return None
    _binding(broker, manager, plan["binding"])
    progress = receipt.setdefault("post_preferences_progress", {})
    if not isinstance(progress, dict):
        raise PreferencesPending("ui_preferences_progress_invalid")
    if progress.get("rollback_required"):
        _rollback(broker, manager, plan, progress, store, key, receipt, deadline)
        raise PreferencesPending("ui_preferences_rolled_back")
    observed = _helper(broker, manager, "read", None, deadline)
    if not _matches(observed, plan["target"]):
        if progress.get("status") == "applying":
            # An interrupted write has unknown outcome. Restore first, never
            # stack another apply onto a partially modified configuration.
            _rollback(broker, manager, plan, progress, store, key, receipt, deadline)
            raise PreferencesPending("ui_preferences_rolled_back")
        progress.update(status="applying", target_sha256=plan["target_sha256"], started_at=_now())
        store.save(key, receipt)
        try:
            _binding(broker, manager, plan["binding"])
            _helper(broker, manager, "apply", plan["target"], deadline)
            observed = _helper(broker, manager, "read", None, deadline)
            if not _matches(observed, plan["target"]):
                raise PreferencesPending("ui_preferences_apply_readback_mismatch")
        except Exception as error:
            progress["error"] = getattr(error, "code", "ui_preferences_apply_interrupted")
            progress["rollback_required"] = True
            store.save(key, receipt)
            try:
                _rollback(broker, manager, plan, progress, store, key, receipt, deadline)
            except Exception:
                # The pinned snapshot survives even when the helper/transport
                # is unavailable; the next call resumes rollback before apply.
                raise PreferencesPending("ui_preferences_rollback_pending") from None
            raise PreferencesPending("ui_preferences_apply_failed_rolled_back") from None
    _binding(broker, manager, plan["binding"])
    result = {"ok": True, "verified": True, "independent_sim_region": plan["independent_sim_region"],
              "target": copy.deepcopy(plan["target"]), "observed": observed,
              "target_sha256": plan["target_sha256"]}
    progress.update(status="completed", completed_at=progress.get("completed_at", _now()), result=result)
    store.save(key, receipt)
    _atomic(Path(state_dir) / (plan["binding"]["instance_id"] + ".current.json"),
            {"schema": SCHEMA, "binding": plan["binding"], "status": "applied", "result": result})
    return result


def restore(broker, manager, binding=None, deadline=None, *, baseline_path=None, state_dir=STATE):
    """Restore the first captured user configuration, including absent settings.

    Called explicitly under the native lock; this does not start GEN, change
    native identity, or apply any profile/display/CPU settings.
    """
    binding = _binding(broker, manager, binding)
    path = Path(baseline_path) if baseline_path is not None else Path(state_dir) / (binding["instance_id"] + ".baseline.json")
    snapshot = _validate_baseline(_read(path), binding)
    _binding(broker, manager, binding)
    _helper(broker, manager, "restore", snapshot, deadline)
    actual = _helper(broker, manager, "read", None, deadline)
    if actual != snapshot:
        raise PreferencesPending("ui_preferences_rollback_readback_mismatch")
    _binding(broker, manager, binding)
    result = {"ok": True, "restored": True, "verified": True, "snapshot": actual}
    _atomic(Path(state_dir) / (binding["instance_id"] + ".current.json"),
            {"schema": SCHEMA, "binding": binding, "status": "restored", "restored_at": _now()})
    return result
