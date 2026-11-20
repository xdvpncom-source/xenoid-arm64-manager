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
import sys
import tempfile


DEFAULT_CONFIG = {"enabled": True, "fps": 20, "scale": "minimum",
                  "min_side": 200, "dpi": 72, "timeout": 180}
CONFIG = Path("/etc/xenoid-fleet/qa-resources.json")
STATE = Path("/var/lib/xenoid-fleet/qa-resources")
UPSTREAM = Path("/opt/xenoid-fleet/upstream")


def validate_config(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
        raise ValueError("Unknown QA resource settings")
    config = {**DEFAULT_CONFIG, **value}
    if type(config["enabled"]) is not bool:
        raise ValueError("enabled must be Boolean")
    fps = config["fps"]
    if fps is not None and (type(fps) is not int or not 0 <= fps <= 1_000_000_000):
        raise ValueError("fps must be null or a nonnegative integer with a nanosecond period")
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


def load_config(path=CONFIG):
    return validate_config(json.loads(path.read_text()))


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


def apply_metrics(broker, config, backup_path):
    if config["scale"] is None:
        return {"changed": False, "preserved": True}
    timeout = config["timeout"] or None
    before = display_state(broker, timeout)
    desired_size, desired_density, native = desired_metrics(before, config)
    if not backup_path.exists():
        atomic_json(backup_path, before)
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


def reconcile(name, config, *, force=False):
    # Timer only handles already-running registered runtimes. It never starts a
    # stopped device and never imports/changes an APK or the desired proxy source.
    manager = native_manager(name)
    owned, reason = manager._owned_container_record(timeout=8)
    if owned is None:
        return {"instance": name, "skipped": "no_owned_runtime"}
    if (owned.get("State") or {}).get("Running") is not True:
        return {"instance": name, "skipped": "stopped"}
    key = {"container_id": owned["Id"], "started_at": owned["State"].get("StartedAt"),
           "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}
    marker = STATE / (name + ".json")
    try:
        previous = json.loads(marker.read_text())
    except (OSError, ValueError):
        previous = {}
    same_boot = previous.get("key") == key
    if not force and same_boot and not previous.get("proxy_pending"):
        return {"instance": name, "skipped": "already_applied"}
    from script_host import Broker
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
    result = broker.set_frame_rate(config["fps"], timeout=config["timeout"])
    metrics = apply_metrics(broker, config, STATE / (name + "-original-display.json"))
    if result.get("proxy", {}).get("ready") is False:
        # Leave the marker incomplete so the timer can retry native recovery.
        # No direct-network bypass or changes to the saved proxy policy.
        document = {"instance": name, "ok": False, "key": key, "proxy_pending": True,
                    "code": "proxy_recovery_pending", "display": result, "metrics": metrics}
        atomic_json(marker, document)
        return document
    document = {"instance": name, "ok": True, "key": key, "display": result, "metrics": metrics}
    atomic_json(marker, document)
    return document


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
                # Off + explicit Apply is a barrier for an older timer that
                # already began changing graphics. Wait for its device lock
                # before reporting disabled, so panel rollback can follow.
                barrier = args.force and not config["enabled"]
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if barrier else fcntl.LOCK_NB))
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
            except Exception:
                # Native exception strings can contain credentials.
                result = {"instance": name, "ok": False, "code": "qa_resource_reconcile_failed"}
            failed |= result.get("ok") is False
            print(json.dumps(result))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
