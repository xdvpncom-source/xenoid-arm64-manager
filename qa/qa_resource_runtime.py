"""Panel-controlled resource defaults, applied once per container boot/configuration."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


DEFAULT_CONFIG = {"enabled": True, "fps": 20, "scale": "minimum",
                  "min_side": 200, "dpi": 72, "timeout": 180, "render_fps": None}
CONFIG = Path("/etc/xenoid-fleet/qa-resources.json")
STATE = Path("/var/lib/xenoid-fleet/qa-resources")
UPSTREAM = Path("/opt/xenoid-fleet/upstream")
PROFILE_POLICY = Path("/etc/xenoid-fleet/device-profile-policy.json")
PROFILE_STATE = Path("/var/lib/xenoid-fleet/device-profiles")


def validate_config(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
        raise ValueError("Unknown QA resource settings")
    config = {**DEFAULT_CONFIG, **value}
    if type(config["enabled"]) is not bool:
        raise ValueError("enabled must be Boolean")
    fps = config["fps"]
    if fps is not None and (type(fps) is not int or not 0 <= fps <= 1_000_000_000):
        raise ValueError("fps must be null or a nonnegative integer with a nanosecond period")
    render_fps = config["render_fps"]
    if render_fps is not None and (type(render_fps) is not int or render_fps != 15
            or config["fps"] != 60 or config["scale"] is not None or config["dpi"] != 420):
        raise ValueError("Fresh native pacing requires render_fps=15, fps=60, scale=null and dpi=420")
    scale = config["scale"]
    if scale is not None and scale != "minimum":
        try:
            valid_scale = type(scale) in (int, float) and math.isfinite(scale) and scale > 0
        except OverflowError:
            valid_scale = False
        if not valid_scale:
            raise ValueError("scale must be null, minimum or a positive finite number")
    for key in ("min_side", "dpi"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(key + " must be a positive integer")
    if type(config["timeout"]) is not int or config["timeout"] < 0:
        raise ValueError("timeout must be a nonnegative integer; zero is unlimited")
    return config


def load_config(path=None):
    return validate_config(json.loads((CONFIG if path is None else path).read_text()))


def _private_json(path, *, maximum):
    """Read a bounded, owner-only settings document without following links."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_mode & 0o077 or before.st_nlink != 1
            or not 0 < before.st_size <= maximum):
        raise ValueError("Unsafe device profile settings file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError("Device profile settings changed during read")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Device profile settings changed during read")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Device profile settings must be an object")
    return value


def effective_config(config, manager, owned):
    """Use the last officially applied profile's display while policy is enabled.

    Display dimensions and FPS come from the committed profile only after its
    immutable runtime binding and semantic checksum match. The resource JSON
    provides the initial/fallback defaults; a copied state file cannot steer
    another device.
    Internal explicit_size is deliberately not accepted by validate_config().
    """
    result = dict(config)
    policy = _private_json(PROFILE_POLICY, maximum=256 * 1024)
    if policy is None:
        return result
    if (policy.get("schema") != "dev.xenoid.profile-policy/v1"
            or type(policy.get("enabled")) is not bool):
        raise ValueError("Invalid device profile policy")
    if not policy["enabled"]:
        return result
    instance = manager.context.instance_id
    if not isinstance(instance, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", instance) is None:
        raise ValueError("Invalid native instance identity")
    applied = _private_json(PROFILE_STATE / (instance + ".json"), maximum=512 * 1024)
    if applied is None:
        return result
    if applied.get("schema") != "dev.xenoid.applied-profile/v1":
        raise ValueError("Invalid applied device profile schema")
    binding = {"instance_id": instance, "resource_tag": manager.context.resource_tag,
               "container_id": owned["Id"]}
    if any(applied.get(key) != value for key, value in binding.items()):
        # A new/replaced native runtime must receive its own profile before it
        # can inherit generated dimensions from a prior container.
        return result
    profile = applied.get("profile")
    if not isinstance(profile, dict) or profile.get("schema") != "dev.xenoid.fingerprint/v1":
        raise ValueError("Invalid committed native device profile")
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    if applied.get("profile_sha256") != digest:
        raise ValueError("Applied device profile checksum mismatch")
    display = profile.get("display")
    if not isinstance(display, dict):
        raise ValueError("Applied device profile has no display settings")
    width, height, dpi = (display.get(key) for key in ("width", "height", "densityDpi"))
    if (any(type(value) is not int or value <= 0 for value in (width, height, dpi))
            or min(width, height) < 2):
        raise ValueError("Invalid applied device display dimensions")
    native_input = profile.get("input", {})
    if not isinstance(native_input, dict):
        raise ValueError("Applied native input bounds are unavailable")
    for axis, maximum in (("x", width - 1), ("y", height - 1)):
        if native_input.get(axis) != {"minimum": 0, "maximum": maximum}:
            raise ValueError("Applied display and native input " + axis + " bounds disagree")
    fps, peak = display.get("defaultRefreshRateHz"), display.get("peakRefreshRateHz")
    if (type(fps) is not int or not 0 < fps <= 1_000_000_000
            or type(peak) not in (int, float) or not math.isfinite(peak) or peak < fps):
        raise ValueError("Invalid applied device display refresh rate")
    result.update(explicit_size=[width, height], dpi=dpi, fps=fps,
                  profile_sha256=digest, profile_policy_revision=applied.get("policy_revision"))
    return result


def pending_profile(manager, owned):
    """A persisted profile transaction owns display until it commits."""
    instance = manager.context.instance_id
    if not isinstance(instance, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", instance) is None:
        raise ValueError("Invalid native instance identity")
    pending = _private_json(PROFILE_STATE / (instance + ".pending.json"), maximum=64 * 1024)
    if pending is None:
        return False
    if (pending.get("schema") != "dev.xenoid.pending-profile/v1"
            or not isinstance(pending.get("operation_key"), str)
            or not pending["operation_key"]
            or re.fullmatch(r"[0-9a-f]{64}", str(pending.get("target_sha256", ""))) is None):
        raise ValueError("Invalid pending device profile schema")
    binding = {"instance_id": instance, "resource_tag": manager.context.resource_tag,
               "container_id": owned["Id"]}
    return all(pending.get(key) == value for key, value in binding.items())


def atomic_json(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        try:
            json.dump(document, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
            os.replace(stream.name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stream.name)


def desired_metrics(before, config):
    if "explicit_size" in config:
        size, dpi = config["explicit_size"], config["dpi"]
        if (not isinstance(size, (list, tuple)) or len(size) != 2
                or any(type(n) is not int or n <= 0 for n in (*size, dpi))):
            raise ValueError("Invalid internal device profile display metrics")
        return tuple(size), dpi, False
    scale = config["scale"]
    if scale is None:
        return None
    physical = before["physical_size"]
    density = before["physical_density"]
    if (len(physical) != 2 or any(type(n) is not int or n <= 0 for n in physical)
            or type(density) is not int or density <= 0):
        raise ValueError("Android returned invalid physical display metrics")
    minimum = scale == "minimum"
    native = not minimum and scale == 1
    if native:
        return tuple(physical), density, True
    factor = max(config["min_side"] / n for n in physical) if minimum else scale
    products = [value * factor for value in (*physical, density)]
    if not all(math.isfinite(value) for value in products):
        raise ValueError("scale produces invalid display metrics")
    lower = max(2, (config["min_side"] + 1) // 2 * 2) if minimum else 2
    size = tuple(max(lower, round(n / 2) * 2) for n in products[:2])
    return size, config["dpi"] if minimum else max(1, round(products[2])), False


def display_state(broker, timeout):
    size = broker.owned_call(["shell", "wm", "size"], timeout)
    density = broker.owned_call(["shell", "wm", "density"], timeout)
    if not size["ok"] or not density["ok"]:
        raise RuntimeError("Android display metrics are unavailable")
    def read(text, kind, override=False):
        label = "Override" if override else "Physical"
        pattern = label + (r" size:\s*(\d+)x(\d+)" if kind == "size" else r" density:\s*(\d+)")
        match = re.search(pattern, text)
        if not match:
            if override:
                return None
            raise RuntimeError("Android did not report physical " + kind)
        values = tuple(map(int, match.groups()))
        return values if kind == "size" else values[0]
    physical_size = read(size["stdout"], "size")
    physical_density = read(density["stdout"], "density")
    override_size = read(size["stdout"], "size", True)
    override_density = read(density["stdout"], "density", True)
    return {"physical_size": physical_size, "physical_density": physical_density,
            "override_size": override_size, "override_density": override_density,
            "size": override_size or physical_size, "density": override_density or physical_density}


def apply_metrics(broker, config, backup_path, *, identity=None):
    if config["scale"] is None and "explicit_size" not in config:
        return {"changed": False, "preserved": True}
    timeout = config["timeout"] or None
    before = display_state(broker, timeout)
    desired_size, desired_density, native = desired_metrics(before, config)
    if not backup_path.exists():
        atomic_json(backup_path, {**before, **({"identity": identity, "identity_binding": "captured"} if identity else {})})
    elif identity:
        original = json.loads(backup_path.read_text())
        if not original.get("identity"):
            # Preserve the pre-v12 original values while binding their migration
            # to the current immutable instance, rather than replacing the data.
            atomic_json(backup_path, {**original, "identity": identity, "identity_binding": "legacy"})
    commands = []
    for kind, desired in (("size", desired_size), ("density", desired_density)):
        override = before["override_" + kind]
        if (native and override is not None) or (not native and before[kind] != desired):
            value = "reset" if native else (f"{desired[0]}x{desired[1]}" if kind == "size" else str(desired))
            commands.append((kind, value))
    attempted = []
    try:
        for kind, value in commands:
            attempted.append(kind)
            result = broker.owned_call(["shell", "wm", kind, value], timeout)
            if not result["ok"]:
                raise RuntimeError("Android rejected display " + kind)
        after = display_state(broker, timeout) if commands else before
        if after["size"] != desired_size or after["density"] != desired_density:
            raise RuntimeError("Android display readback did not match the configuration")
    except BaseException as error:
        rollback_failed = False
        for kind in reversed(attempted):
            original = before["override_" + kind]
            value = "reset" if original is None else (f"{original[0]}x{original[1]}" if kind == "size" else str(original))
            try:
                restored = broker.owned_call(["shell", "wm", kind, value], timeout)
                rollback_failed |= not restored.get("ok")
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise RuntimeError("Display configuration failed; rollback needs attention") from error
        raise
    return {"changed": bool(commands), "size": desired_size, "density": desired_density}


def native_manager(name):
    sys.path.insert(0, str(UPSTREAM / "src"))
    os.chdir(UPSTREAM)
    from xenoid.config import resolve_instance
    from xenoid.backend import RuntimeManager
    context, settings, lease = resolve_instance(name, project_root=UPSTREAM, migrate_legacy=False)
    return RuntimeManager(context, settings, lease)


def resource_identity(manager):
    return {"instance_id": manager.context.instance_id, "resource_tag": manager.context.resource_tag}


def read_marker(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def resource_key(owned, config, identity):
    result = {"container_id": owned["Id"], "started_at": owned["State"].get("StartedAt"),
              "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
              **identity}
    if "profile_sha256" in config:
        result["profile_sha256"] = config["profile_sha256"]
    return result


def prepare_identity(marker, backup, previous, identity):
    original = read_marker(backup)
    known = [document.get("identity") for document in (previous, original) if document.get("identity")]
    if any(value != identity for value in known):
        # Reusing a numeric ID after native deletion creates a different immutable
        # instance. Keep old QA evidence separate from the new instance's original
        # display. Android data and native registry files are never touched here.
        import time
        archive = STATE / "archive" / (marker.stem + "-" + str(time.time_ns()))
        archive.mkdir(mode=0o700, parents=True)
        for path in (marker, backup):
            if path.exists():
                os.replace(path, archive / path.name)
        return {}
    return previous


def ensure_host_vm_guard(manager, owned):
    # This runs under the same Native instance lock as the display transaction.
    # Even an already-applied display marker must not conceal a lost per-boot
    # producer bind after an out-of-band Docker restart. No host sysctl reset is
    # performed by the timer; only the known Android producer is isolated.
    from qa_host_memory import ensure_guest_guard
    result = ensure_guest_guard(manager, owned["Id"])
    if result.get("ok") is not True:
        raise RuntimeError("Host VM producer isolation did not complete")
    return result


def reconcile(name, config, *, force=False):
    # Timer only handles already-running registered runtimes. It never starts a
    # stopped device and never imports/changes an APK or the desired proxy source.
    manager = native_manager(name)
    owned, reason = manager._owned_container_record(timeout=8)
    if owned is None:
        return {"instance": name, "skipped": "no_owned_runtime"}
    if (owned.get("State") or {}).get("Running") is not True:
        return {"instance": name, "skipped": "stopped"}
    # This is a root QA resource transaction, with an administrator-selected
    # wait budget. Hold the native lock across FPS, profile replay, proxy recovery
    # and WM writes. Its ContextVar makes Broker's inner lock reentrant. The
    # ordinary script SDK keeps its original lock behavior and is not modified.
    with manager.instance_operation_lock(timeout_seconds=config["timeout"] or None):
        return reconcile_owned(name, config, manager, force=force)


def reconcile_owned(name, config, manager, *, force=False, broker=None):
    # A lifecycle operation may have run while waiting for the native lock.
    # Refresh all binding/marker information before any resource mutation.
    owned, reason = manager._owned_container_record(timeout=8)
    if owned is None:
        return {"instance": name, "skipped": "no_owned_runtime"}
    if (owned.get("State") or {}).get("Running") is not True:
        return {"instance": name, "skipped": "stopped"}
    # An explicit native Bluejay image must never fall back to the old HWC
    # replacement/WM-scaling path after a legacy settings update.
    from qa_fresh6a import is_bluejay
    cold = getattr(getattr(manager, "cfg", None), "cold_profile", None)
    if is_bluejay(cold) and config.get("render_fps") != 15:
        raise ValueError("Fresh Pixel 6a requires its separate native render_fps=15 contract")
    if pending_profile(manager, owned):
        return {"instance": name, "ok": True, "skipped": "profile_pending", "deferred": True}
    config = effective_config(config, manager, owned)
    host_vm = ensure_host_vm_guard(manager, owned)
    identity = resource_identity(manager)
    key = resource_key(owned, config, identity)
    marker = STATE / (name + ".json")
    backup = STATE / (name + "-original-display.json")
    previous = prepare_identity(marker, backup, read_marker(marker), identity)
    same_boot = previous.get("key") == key
    if not force and same_boot and not previous.get("proxy_pending"):
        return {"instance": name, "skipped": "already_applied", "host_vm": host_vm}
    from script_host import Broker
    if broker is None:
        broker = Broker(manager, lambda *_args: None, config={"expected_container_id": owned["Id"],
                                                           "command_timeout": config["timeout"]})
    if not force and same_boot and previous.get("proxy_pending"):
        # Display succeeded on the previous attempt. Retry only the native
        # network recovery, leaving later script-selected metrics/FPS alone.
        import time
        deadline = time.monotonic() + config["timeout"] if config["timeout"] else None
        with broker.owned_manager() as owned_manager:
            proxy = broker.display_proxy_reconcile(owned_manager, deadline)
        previous["proxy_pending"] = not proxy.get("ready")
        previous["ok"] = not previous["proxy_pending"]
        previous["display"]["proxy"] = proxy
        atomic_json(marker, previous)
        return {**previous, "proxy_retry_only": True}
    if config.get("render_fps") is not None:
        # Native image/boot owns the 15 FPS render cap independently of the
        # advertised 60 Hz mode. Never invoke the legacy HWC replacement here.
        import time
        from qa_fresh6a import verify_runtime
        deadline = time.monotonic() + config["timeout"] if config["timeout"] else None
        # A timer-created Broker binds its immutable CID lazily. Enter the
        # official owner context before comparing IDs or reading Android.
        with broker.owned_manager() as active:
            result = verify_runtime(broker, active, deadline=deadline)
        metrics = {"changed": False, "preserved": True, "native": True}
    else:
        result = broker.set_frame_rate(config["fps"], timeout=config["timeout"])
        metrics = apply_metrics(broker, config, backup, identity=identity)
    if result.get("proxy", {}).get("ready") is False:
        # Leave the marker incomplete so the timer can retry native recovery.
        # No direct-network bypass or changes to the saved proxy policy.
        document = {"instance": name, "ok": False, "key": key, "identity": identity, "proxy_pending": True,
                    "code": "proxy_recovery_pending", "display": result, "metrics": metrics}
        atomic_json(marker, document)
        return document
    document = {"instance": name, "ok": True, "key": key, "identity": identity, "display": result, "metrics": metrics}
    atomic_json(marker, document)
    return document


def apply_for_broker(broker, timeout=None):
    """SDK entry point: verify central defaults on each inherited scenario step."""
    config = load_config()
    if timeout is not None:
        if type(timeout) is not int or timeout < 0:
            raise ValueError("Resource timeout must be nonnegative or null")
        config = {**config, "timeout": timeout}
    if not config["enabled"]:
        return {"ok": True, "skipped": "disabled"}
    manager = broker.verify() if broker.verify else broker.manager
    with manager.instance_operation_lock(timeout_seconds=config["timeout"] or None):
        # Broker performs its existing immutable container validation under the
        # same reentrant native lock before any display/VM operation.
        with broker.owned_manager() as current:
            folder = getattr(getattr(broker, "regeneration_receipts", None), "folder", None)
            number = folder.name if isinstance(folder, Path) else ""
            name = "phone-" + number if re.fullmatch(r"[1-9][0-9]*", number) else "instance-" + current.context.instance_id
            STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Unlike the timer, an explicit inherited scenario step verifies
            # display readback even when a soft reboot kept Docker StartedAt.
            result = reconcile_owned(name, config, current, force=True, broker=broker)
    if result.get("deferred") is True:
        raise RuntimeError("A native device profile transaction is still pending")
    if result.get("ok") is not True:
        raise RuntimeError(result.get("code", "Central device resource settings are not ready"))
    if broker.log:
        selected = "generated profile" if "profile_sha256" in result.get("key", {}) else "panel defaults"
        broker.log("action", "Device resource settings verified from " + selected)
    return result


def registry_names():
    sys.path.insert(0, str(UPSTREAM / "src"))
    os.chdir(UPSTREAM)
    # Read the same upstream registry through the native CLI; avoid assuming an
    # on-disk registry layout or probing unrelated Docker containers.
    import subprocess
    result = subprocess.run([str(UPSTREAM / "xenoid"), "instance", "list"],
                            capture_output=True, text=True, check=False,
                            env=dict(os.environ, XENOID_PROJECT=str(UPSTREAM)))
    document = json.loads(result.stdout)
    if result.returncode or document.get("ok") is not True:
        raise RuntimeError("Could not read the native registry")
    return [row["instanceName"] for row in document["instances"]
            if re.fullmatch(r"phone-[1-9][0-9]*", str(row.get("instanceName", "")))]


def safe_failure_reason(error):
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        return code
    return type(error).__name__


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--device", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run QA resource management as root")
    if args.configure:
        config = validate_config(json.load(sys.stdin))
        atomic_json(CONFIG, config)
        print(json.dumps({"ok": True, "config": config}))
        return 0
    config = load_config()
    if not config["enabled"] and not args.force:
        print(json.dumps({"ok": True, "skipped": "disabled"}))
        return 0
    STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    names = registry_names() if args.all else args.device
    failed = False
    for name in names:
        if not re.fullmatch(r"phone-[1-9][0-9]*", name):
            raise ValueError("Invalid native instance name")
        # Nonblocking per-device timer lock avoids piling up slow boot attempts;
        # native operation locks and container identity checks remain in Broker.
        with open(STATE / (name + ".lock"), "a") as lock:
            try:
                # Explicit lifecycle/Apply operations wait for an older timer
                # before converging, or reporting disabled for panel rollback.
                # Timer attempts stay nonblocking and never pile up.
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.force else fcntl.LOCK_NB))
            except BlockingIOError:
                # A timer may skip an overlapping attempt; an explicit panel
                # apply must not claim completion while another apply is busy.
                print(json.dumps({"instance": name, "skipped": "busy", "ok": not args.force}))
                failed |= args.force
                continue
            try:
                # A rollback or panel save may disable/change defaults while a
                # previous device is converging. Re-read under this device's
                # lock so a stale timer snapshot cannot reapply after rollback.
                config = load_config()
                if not config["enabled"]:
                    print(json.dumps({"instance": name, "skipped": "disabled"}))
                    continue
                result = reconcile(name, config, force=args.force)
            except Exception as error:
                # Native exception strings can contain credentials.
                result = {"instance": name, "ok": False, "code": "qa_resource_reconcile_failed",
                          "reason": safe_failure_reason(error)}
            failed |= result.get("ok") is False
            print(json.dumps(result))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
