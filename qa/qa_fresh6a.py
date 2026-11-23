"""Fresh Pixel 6a policy defaults and read-only runtime contract checks.

Native source/image construction owns Build, display, boot properties and
render pacing. This module neither invents device IDs nor modifies Android.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

DEFAULTS_PATH = Path(__file__).with_name("default-bluejay-policy.json")
RENDER_MANIFEST = "/system/etc/xenoid/fresh-bluejay-render.json"
PACING = {"debug.hwc.present_fps": "15", "debug.gui.frame_pacer_fps": "15", "debug.hwui.fps_divisor": "4"}
CONFIG_FILES = ("/vendor/etc/SwiftShader.ini", "/vendor/sw.ini")
CONFIG_SHA256 = "228340162de0f5f5ac4ca228a89f6256cc8cc9cdaee81b618ebceec2f1b2dba8"


def defaults():
    value = json.loads(DEFAULTS_PATH.read_text())
    if value.get("schema") != "dev.xenoid.fresh-bluejay-defaults/v1":
        raise ValueError("Invalid fresh Pixel 6a defaults")
    return value


def is_bluejay(profile):
    build = profile.get("build", {}) if isinstance(profile, dict) else {}
    return build.get("model") == "Pixel 6a" and build.get("device") == "bluejay"


def validate_profile_contract(profile):
    """Verify the selected native release without claiming physical hardware."""
    spec = defaults()
    if not is_bluejay(profile):
        raise ValueError("A fresh Pixel 6a native profile is required")
    for key, expected in spec["build"].items():
        if profile.get("build", {}).get(key) != expected:
            raise ValueError("Fresh Pixel 6a Build metadata disagrees: " + key)
    display = profile.get("display", {})
    if any(display.get(key) != expected for key, expected in spec["display"].items()):
        raise ValueError("Fresh Pixel 6a display contract disagrees")
    if (display.get("supportedRefreshRatesHz") != [60]
            or display.get("modes") != [{"id": 1, "width": 1080, "height": 2400, "refreshRateHz": 60}]):
        raise ValueError("Fresh Pixel 6a mode catalog disagrees")
    native_input = profile.get("input", {})
    if native_input.get("x") != {"minimum": 0, "maximum": 1079} or native_input.get("y") != {"minimum": 0, "maximum": 2399}:
        raise ValueError("Fresh Pixel 6a official input axes disagree")
    for key in ("capacityMah", "minimumCapacityMah", "chargeFullDesignUah", "chargeFullUah"):
        if profile.get("battery", {}).get(key) != spec["battery"][key]:
            raise ValueError("Fresh Pixel 6a battery capacity disagrees: " + key)
    if profile.get("battery", {}).get("plugged") != 0 or profile["battery"].get("status") != 3:
        raise ValueError("Fresh Pixel 6a retains the observed unplugged/discharging battery state")
    return spec


def _leaves(value, prefix=""):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _leaves(value[key], prefix + "." + key if prefix else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, prefix + "." + str(index))
    else:
        yield prefix, value


def default_policy(profile):
    """Fixed authentic release/display, changing supported battery telemetry.

    All profile leaves remain visible. Native IDs retain native ownership, while
    camera/sensors, CPU/RAM, USB hardware and Build metadata have no random pool.
    This is a new default only; it does not migrate saved administrator policies.
    """
    spec = validate_profile_contract(profile)
    # Existing panel schema keeps unsupported/native-owned rows enabled with
    # no choices. Mutable fields we deliberately freeze are disabled instead.
    from qa_device_profile import field_catalog
    catalog = {row["path"]: row for row in field_catalog(profile)}
    fields = {path: {"enabled": not catalog[path]["mutable"], "values": []}
              for path, _ in _leaves(profile)}
    for path in fields:
        if path.startswith("ids.") or path == "usb.serial":
            fields[path]["enabled"] = True
    battery = spec["battery"]
    pools = {"level": battery["levels"], "voltage": battery["voltages"], "temperature": battery["temperatures"]}
    for name, values in pools.items():
        fields["battery." + name] = {"enabled": True, "values": list(values)}
    levels = sorted(set([profile["battery"]["level"], *battery["levels"]]))
    fields["battery.chargeCounterUah"] = {"enabled": True,
        "values": [battery["chargeFullUah"] * level // 100 for level in levels]}
    for zone, values in spec["thermal_temperatures"].items():
        path = "thermal." + zone + ".temp"
        if path in fields:
            fields[path] = {"enabled": True, "values": list(values)}
    return {"schema": "dev.xenoid.profile-policy/v1", "enabled": True, "revision": 1, "fields": fields}


def validate_policy_contract(policy, profile):
    """Reject old random identity/geometry pools before a fresh native GEN.

    Administrators switch policies explicitly; existing saved documents are not
    rewritten. Fixed fields may remain checked with an empty/identical pool.
    """
    baseline = default_policy(profile)
    if any(policy.get(name, {}).get("enabled") is True for name in ("display_random", "display_presets")):
        raise ValueError("Fresh Pixel 6a requires its native display tuple")
    values = dict(_leaves(profile))
    for path, row in policy.get("fields", {}).items():
        default = baseline["fields"].get(path)
        if (default is not None and not default["enabled"] and row.get("enabled") is True
                and any(value != values.get(path) for value in row.get("values", []))):
            raise ValueError("Fresh Pixel 6a fixed field has a random choice: " + path)
    return policy


def _require(ok, code):
    if not ok:
        raise RuntimeError(code)


def verify_actual_caps(owned, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    """Require real cgroup limits, not just Docker's desired HostConfig."""
    cid = owned.get("Id", "")
    pid = owned.get("State", {}).get("Pid")
    _require(re.fullmatch("[0-9a-f]{64}", str(cid)) and type(pid) is int and pid > 0,
             "fresh6a_runtime_pid_invalid")
    rows = (proc_root / str(pid) / "cgroup").read_text().splitlines()
    paths = [line[3:] for line in rows if line.startswith("0::")]
    _require(len(paths) == 1 and paths[0].startswith("/") and cid in Path(paths[0]).name
             and ".." not in Path(paths[0]).parts, "fresh6a_cgroup_owner_changed")
    group = cgroup_root / paths[0].lstrip("/")
    cpu = (group / "cpu.max").read_text().split()
    _require(len(cpu) == 2 and all(value.isdecimal() for value in cpu)
             and int(cpu[0]) == int(cpu[1]) > 0
             and (group / "memory.max").read_text().strip() == "2147483648"
             and (group / "memory.swap.max").read_text().strip() == "536870912", "fresh6a_actual_resource_caps_changed")
    return {"cpu_cores": 1, "memory_bytes": 2147483648, "swap_bytes": 536870912}


def verify_config_files(root, assets):
    """Both loader-compatible paths are immutable regular copies, not links."""
    _require(all(assets.get(path) == CONFIG_SHA256 for path in CONFIG_FILES),
             "fresh6a_swiftshader_manifest_hash_changed")
    for path in CONFIG_FILES:
        file = root / path.lstrip("/")
        info = file.lstat()
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0
                 and info.st_nlink == 1 and not info.st_mode & 0o022,
                 "fresh6a_swiftshader_regular_file_required:" + path)
        _require(hashlib.sha256(file.read_bytes()).hexdigest() == CONFIG_SHA256,
                 "fresh6a_swiftshader_config_changed:" + path)


def verify_runtime(broker, manager, *, deadline):
    """Read existing source-owned native pacing; never install or reconfigure it.

    The public image manifest pins the render bytes. Properties prove configured
    pacing, not measured presentation cadence; qualification tests measure that
    separately. Every command uses the broker's official, owner-bound transport.
    """
    def read(argv):
        result = broker.native_display_exec(manager, argv, deadline=deadline, check=True)
        return result.stdout.decode(errors="strict").strip()

    owned, reason = manager._owned_container_record(timeout=8)
    _require(owned and owned.get("Id") == broker.container_id and owned.get("State", {}).get("Running") is True,
             reason or "fresh6a_owned_runtime_unavailable")
    caps = owned.get("HostConfig", {})
    _require({key: caps.get(key) for key in ("NanoCpus", "Memory", "MemorySwap")} ==
             {"NanoCpus": 1000000000, "Memory": 2147483648, "MemorySwap": 2684354560}, "fresh6a_resource_caps_changed")
    resources = verify_actual_caps(owned)
    profile = json.loads(read(["cat", "/data/local/tmp/xenoid-profile/effective.json"]))
    validate_profile_contract(profile)
    from xenoid.cold_profile import expected_properties
    expected_map = expected_properties(profile)
    _require(len(expected_map) == 191, "fresh6a_build_property_contract_changed")
    selected = {}
    for line in read(["getprop"]).splitlines():
        match = re.fullmatch(r"\[([^]\r\n]+)\]: \[(.*)\]", line)
        if match and match[1] in expected_map:
            _require(match[1] not in selected, "fresh6a_duplicate_build_property")
            selected[match[1]] = match[2]
    _require(selected == expected_map, "fresh6a_live_build_properties_changed")
    _require(read(["wm", "size"]) == "Physical size: 1080x2400"
             and read(["wm", "density"]) == "Physical density: 420", "fresh6a_native_geometry_changed")
    period = read(["dumpsys", "SurfaceFlinger", "--latency"]).splitlines()[0]
    _require(period in {"16666666", "16666667"}, "fresh6a_advertised_refresh_changed")
    for key, expected in PACING.items():
        _require(read(["getprop", key]) == expected, "fresh6a_render_property_changed:" + key)
    manifest = json.loads(read(["cat", RENDER_MANIFEST]))
    _require(manifest.get("schema") == "xenoid.fresh-bluejay-render/v1"
             and manifest.get("advertised_hz") == 60 and manifest.get("render_fps") == 15
             and manifest.get("properties") == PACING
             and manifest.get("display") == {"width": 1080, "height": 2400, "densityDpi": 420, "physicalPpi": 429},
             "fresh6a_render_manifest_invalid")
    _require(manifest.get("swiftshader_threads") == 1
             and manifest.get("regular_config_files") == list(CONFIG_FILES)
             and "symlinks" not in manifest, "fresh6a_swiftshader_config_manifest_changed")
    assets = manifest.get("artifactSha256")
    required = {"/system/lib64/libgui.so", "/system/lib64/libgui_pacer.so", "/vendor/lib64/hw/vulkan.pastel.so"}
    _require(isinstance(assets, dict) and required <= set(assets), "fresh6a_render_assets_missing")
    verify_config_files(Path("/proc") / str(owned["State"]["Pid"]) / "root", assets)
    for path, expected in assets.items():
        _require(isinstance(path, str) and path.startswith(("/system/", "/vendor/"))
                 and not any(part in {"", ".", ".."} for part in path.split("/")[1:])
                 and re.fullmatch("[0-9a-f]{64}", str(expected)), "fresh6a_render_asset_invalid")
        output = read(["sha256sum", path]).split()
        _require(len(output) == 2 and output == [expected, path], "fresh6a_render_asset_changed:" + path)
    mapping = broker.native_interaction_snapshot(manager, deadline)
    _require(mapping.get("ready") is True and mapping.get("mapping", {}).get("logical_size") == [1080, 2400],
             "fresh6a_official_input_not_ready")
    current, reason = manager._owned_container_record(timeout=8)
    _require(current and all(current.get(key) == owned.get(key) for key in ("Id", "Image"))
             and current.get("State", {}).get("Pid") == owned.get("State", {}).get("Pid")
             and current.get("State", {}).get("StartedAt") == owned.get("State", {}).get("StartedAt")
             and current.get("State", {}).get("Running") is True, "fresh6a_runtime_changed_during_readback")
    return {"ok": True, "fps": 60, "render_fps": 15, "configured_pacing_verified": True,
            "real_caps": resources, "build_properties_verified": len(expected_map),
            "presentation_cadence_measured": False, "changed": False, "restarted": False,
            "container_id": broker.container_id, "official_input_unchanged": True,
            "native_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def main(argv=None):
    """Explicit policy generation for fresh installation/reset; stdout only."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["emit-policy"])
    parser.add_argument("--template", required=True)
    parser.add_argument("--revision", type=int, default=1)
    args = parser.parse_args(argv)
    if args.revision < 1:
        parser.error("revision must be positive")
    from qa_device_profile import validate_profile, validate_policy
    profile = validate_profile(json.loads(Path(args.template).read_text()))
    policy = default_policy(profile)
    policy["revision"] = args.revision
    validate_policy(policy, [profile])
    print(json.dumps(policy, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
