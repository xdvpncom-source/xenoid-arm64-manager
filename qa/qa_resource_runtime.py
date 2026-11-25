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
                  "min_side": 200, "dpi": 72, "timeout": 180, "render_fps": None, "memory_reserve_mib": 0,
                  "proxy_maintenance_timeout": 180}
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
    reserve = config["memory_reserve_mib"]
    if "memory_reserve_mib" not in value:
        reserve = 256 if render_fps is not None else 0
        config["memory_reserve_mib"] = reserve
    if type(reserve) is not int or not 0 <= reserve <= 1024:
        raise ValueError("memory_reserve_mib must be an integer from 0 through 1024")
    if reserve and render_fps is None:
        raise ValueError("Memory reclaim reserve requires the fresh native resource contract")
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
    if type(config["proxy_maintenance_timeout"]) is not int or config["proxy_maintenance_timeout"] < 0:
        raise ValueError("proxy_maintenance_timeout must be a nonnegative integer; zero is unlimited")
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
    # Match the official CLI before native modules cache artifact tool inputs.
    from script_host import adopt_native_environment
    adopt_native_environment()
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
    # Network maintenance budgets must not invalidate an existing graphics
    # marker or cause a legacy display to be reapplied.
    display_config = {k: v for k, v in config.items() if k != "proxy_maintenance_timeout"}
    result = {"container_id": owned["Id"], "started_at": owned["State"].get("StartedAt"),
              "config_sha256": hashlib.sha256(json.dumps(display_config, sort_keys=True).encode()).hexdigest(),
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


def native_proxy_maintenance(manager, owned, config, document, *, broker=None):
    """Ordinary fresh-native timer only; network pending is not graphics failure.

    The existing caller owns the native operation lock. The existing sixty
    second timer is the retry schedule; no worker or extra timer is created.
    """
    import time
    from script_host import Broker, RuntimeIdentityError
    seconds = config.get("proxy_maintenance_timeout", 180)
    deadline = time.monotonic() + seconds if seconds else None
    result = dict(document)
    summary = {"ready": False, "reconciled": False, "code": "proxy_maintenance_pending"}
    bootstrap_summary = {"ready": False, "attempted": False, "recovered": False,
                         "code": "bootstrap_status_unreadable"}
    bootstrap_attempted = False
    if broker is None:
        broker = Broker(manager, lambda *_args: None,
                        config={"expected_container_id": owned["Id"], "command_timeout": seconds})
    def remaining():
        value = None if deadline is None else deadline - time.monotonic()
        if value is not None and value <= 0:
            raise TimeoutError("proxy_maintenance_timeout")
        return value
    def same_owner(active):
        value = remaining()
        current, _ = active._owned_container_record(timeout=5 if value is None else min(5, value))
        if current is None or current.get("Id") != owned["Id"] or current.get("State", {}).get("Running") is not True:
            raise RuntimeIdentityError("The owned runtime changed during proxy maintenance")
    def bootstrap_status(active):
        # The native client validates instance identity, epoch, component names
        # and typed component states. A failed GET never authorizes recovery.
        value = remaining()
        options = {"timeout": 3 if value is None else min(3, value)}
        status = active.daemon_client(**options).bootstrap_status(**options)
        if not isinstance(status, dict) or status.get("schema") != "dev.xenoid.daemon-bootstrap/v1":
            return None
        return status
    def bootstrap_view(status):
        if status is None:
            return {"ready": False, "attempted": bootstrap_attempted, "recovered": False,
                    "code": "bootstrap_status_unreadable"}
        components = status["components"]
        failed = sorted(name for name, component in components.items()
                        if component.get("ok") is False and component.get("state") in
                        ("failed", "timed_out", "cancelled"))
        ready = status.get("ok") is True and status.get("state") == "ready" and all(
            component.get("ok") is True for component in components.values())
        return {"ready": ready, "attempted": bootstrap_attempted, "recovered": False,
                "state": status["state"], "failed_components": failed}
    try:
        with broker.owned_manager() as active:
            same_owner(active)
            value = remaining()
            options = {} if value is None else {"timeout": value}
            client = active.daemon_client(**options)
            try:
                status = client.proxy_status(**options)
            except RuntimeIdentityError:
                raise
            except Exception:
                status = {}
            if isinstance(status, dict) and status.get("ok") is True and (
                    status.get("enabled") is False or status.get("configured") is False):
                summary = {"ready": None, "reconciled": False, "skipped": "disabled_or_unconfigured",
                           "enabled": status.get("enabled"), "configured": status.get("configured")}
                bootstrap_summary = {"ready": None, "attempted": False, "recovered": False,
                                     "skipped": "proxy_disabled_or_unconfigured"}
            else:
                if not isinstance(status, dict) or not status.get("runtimeEpoch"):
                    # Official control bootstrap can establish the current
                    # daemon epoch; never fabricate one from a saved marker.
                    from xenoid.util import command_timeout
                    value = remaining()
                    budget = contextlib.nullcontext() if value is None else command_timeout(value)
                    bootstrap_attempted = True
                    with budget:
                        bootstrap = active.reconcile_bootstrap(**({} if value is None else {"timeout": remaining()}))
                    if not isinstance(bootstrap, dict) or bootstrap.get("transportReady") is not True or bootstrap.get("controlReady") is not True:
                        summary["code"] = "proxy_bootstrap_pending"
                        same_owner(active)
                        if result.get("code") == "proxy_recovery_pending":
                            result.pop("code")
                        bootstrap_summary.update(attempted=True, code="bootstrap_control_pending")
                        result.update(ok=True, resource_ok=True, proxy_pending=True, proxy_watchdog=summary,
                                      bootstrap_pending=True, bootstrap_watchdog=bootstrap_summary)
                        return result
                same_owner(active)
                summary = broker.display_proxy_reconcile(active, deadline, stopping=False)
                try:
                    observed = bootstrap_status(active)
                    bootstrap_summary = bootstrap_view(observed)
                    if (not bootstrap_attempted and observed is not None and
                            observed.get("runtimeEpoch") != status.get("runtimeEpoch")):
                        summary = {"ok": False, "ready": False, "reconciled": summary.get("reconciled") is True,
                                   "code": "proxy_bootstrap_epoch_unconfirmed"}
                    # A ready proxy cannot hide a terminal failed component.
                    # Retry at most once per ordinary tick, using only the
                    # owning native API. Its same-epoch coordinator preserves
                    # already-ready components and retries failed ones.
                    if (not bootstrap_attempted and observed is not None and
                            observed.get("state") in ("degraded", "failed") and
                            bootstrap_summary["failed_components"]):
                        if observed.get("runtimeEpoch") != status.get("runtimeEpoch"):
                            bootstrap_summary["code"] = "bootstrap_epoch_unconfirmed"
                        else:
                            same_owner(active)
                            from xenoid.util import command_timeout
                            value = remaining()
                            budget = contextlib.nullcontext() if value is None else command_timeout(value)
                            bootstrap_attempted = True
                            # Bootstrap can change control/proxy state or its
                            # userspace epoch. Never retain readiness sampled
                            # before this mutation. The next ordinary tick
                            # obtains fresh daemon AND host proxy proof.
                            summary = {"ok": False, "ready": False, "reconciled": summary.get("reconciled") is True,
                                       "code": "proxy_post_bootstrap_unverified"}
                            with budget:
                                recovered = active.reconcile_bootstrap(**({} if value is None else {"timeout": remaining()}))
                            same_owner(active)
                            fresh = bootstrap_status(active)
                            bootstrap_summary = bootstrap_view(fresh)
                            # API ok/controlReady alone is not aggregate ready.
                            # Require a fresh ready snapshot of its exact result.
                            matched = (fresh is not None and isinstance(recovered, dict) and
                                       recovered.get("ready") is True and recovered.get("state") == "ready" and
                                       type(recovered.get("generation")) is int and
                                       fresh.get("instanceId") == recovered.get("instanceId") and
                                       fresh.get("generation") == recovered.get("generation") and
                                       fresh.get("runtimeEpoch") == recovered.get("runtimeEpoch"))
                            bootstrap_summary["ready"] = bootstrap_summary["ready"] and matched
                            bootstrap_summary["recovered"] = bootstrap_summary["ready"]
                            bootstrap_summary["runtime_epoch_changed"] = (fresh is not None and
                                fresh.get("runtimeEpoch") != observed.get("runtimeEpoch"))
                            if not matched:
                                bootstrap_summary["code"] = "bootstrap_result_unconfirmed"
                except RuntimeIdentityError:
                    raise
                except Exception:
                    bootstrap_summary = {"ready": False, "attempted": bootstrap_attempted, "recovered": False,
                                         "code": "bootstrap_maintenance_pending"}
            same_owner(active)
    except RuntimeIdentityError:
        raise
    except Exception:
        # Do not copy native exception text, source URLs or credentials.
        summary = {"ready": False, "reconciled": False, "code": "proxy_maintenance_pending"}
    pending = not summary.get("skipped") and summary.get("ready") is not True
    if result.get("code") == "proxy_recovery_pending":
        result.pop("code")
    result.update(ok=True, resource_ok=True, proxy_pending=pending, proxy_watchdog=summary,
                  bootstrap_pending=not bootstrap_summary.get("skipped") and bootstrap_summary.get("ready") is not True,
                  bootstrap_watchdog=bootstrap_summary)
    return result


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
    native = is_bluejay(cold) and config.get("render_fps") is not None
    native_timer = not force and native
    if native and any(os.path.lexists(manager.context.state_root / journal) for journal in (
            "device-regenerate-v3.json", "convergence-v1.json", "device-regenerate-v2.json", "device-regenerate.json")):
        return {"instance": name, "ok": True, "skipped": "native_transaction_pending", "deferred": True}
    control_route = None
    if native:
        import time
        from qa_control_routes import ensure_control_routes
        control_route = ensure_control_routes(manager, owned["Id"],
            deadline=time.monotonic() + config["timeout"] if config["timeout"] else None)
    config = effective_config(config, manager, owned)
    host_vm = ensure_host_vm_guard(manager, owned)
    headroom = (ensure_memory_headroom(manager, owned["Id"], reserve_mib=config["memory_reserve_mib"])
                if config.get("render_fps") is not None else {"ok": True, "skipped": "legacy_resources"})
    identity = resource_identity(manager)
    key = resource_key(owned, config, identity)
    marker = STATE / (name + ".json")
    backup = STATE / (name + "-original-display.json")
    previous = prepare_identity(marker, backup, read_marker(marker), identity)
    same_boot = previous.get("key") == key
    if native_timer and same_boot:
        document = native_proxy_maintenance(manager, owned, config,
            {**previous, "instance": name, "host_vm": host_vm, "memory_headroom": headroom, "control_route": control_route}, broker=broker)
        atomic_json(marker, document)
        return {**document, "proxy_maintenance_only": True}
    if not force and same_boot and not previous.get("proxy_pending"):
        return {"instance": name, "skipped": "already_applied", "host_vm": host_vm, "memory_headroom": headroom}
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
                    "code": "proxy_recovery_pending", "display": result, "metrics": metrics, "memory_headroom": headroom}
        atomic_json(marker, document)
        return document
    document = {"instance": name, "ok": True, "key": key, "identity": identity, "display": result, "metrics": metrics, "memory_headroom": headroom}
    if native:
        document["control_route"] = control_route
    if native_timer:
        document = native_proxy_maintenance(manager, owned, config, document, broker=broker)
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




HIGH_BYTES=1792*1024*1024
MAX_BYTES=2*1024*1024*1024
SWAP_BYTES=512*1024*1024

def _memory_need(value,code):
    if not value:raise RuntimeError(code)

def _memory_binding(manager,cid,proc,*,qualified=True):
    _memory_need(re.fullmatch('[0-9a-f]{64}',cid)is not None,'full_CID_required')
    owned,error=manager._owned_container_record(timeout=5)
    _memory_need(owned is not None and owned.get('Id')==cid,'headroom_owner_changed')
    labels=(owned.get('Config')or{}).get('Labels')or{}
    _memory_need(labels.get('dev.xenoid.owner')=='xenoid'
         and labels.get('dev.xenoid.instance_id')==manager.context.instance_id
         and labels.get('dev.xenoid.resource_tag')==manager.lease.resource_tag,'headroom_labels_changed')
    hc=owned.get('HostConfig')or{}
    if qualified:
        _memory_need([hc.get(k)for k in('NanoCpus','Memory','MemorySwap')]==[1000000000,MAX_BYTES,MAX_BYTES+SWAP_BYTES],
             'headroom_requires_qualified_caps')
    state=owned.get('State')or{};pid=state.get('Pid')
    _memory_need(state.get('Running')is True and type(pid)is int and pid>1,'headroom_runtime_not_running')
    base=proc/str(pid)
    ticks=base.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
    expected='/system.slice/docker-'+cid+'.scope'
    if qualified:
        _memory_need(base.joinpath('cgroup').read_text().splitlines()==['0::'+expected],'headroom_cgroup_changed')
    return (cid,owned.get('Image'),pid,state.get('StartedAt'),ticks),expected

def _memory_filesystem_type(fd):
    # cgroup kernfs is legitimately delegated (UID1000/0775 on qualified hosts).
    # Trust the kernel filesystem and exact owned cgroup, not private-file UID
    # conventions. Credentials/receipts retain their separate strict rules.
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True)
    libc.fstatfs.argtypes=[ctypes.c_int,ctypes.c_void_p]
    libc.fstatfs.restype=ctypes.c_int
    data=ctypes.create_string_buffer(256)
    _memory_need(libc.fstatfs(fd,data)==0,'headroom_cgroup_fstatfs_failed')
    return ctypes.c_long.from_buffer(data).value & 0xffffffff


def _memory_record(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(directory)
        _memory_need(info.st_uid==os.geteuid() and stat.S_IMODE(info.st_mode)==0o700,
                     'headroom_receipt_directory_invalid')
        atomic_json(path,document)
        os.fsync(directory)
    finally:os.close(directory)


def _memory_write(fd, payload, record_writer, timeout, *, command=None):
    """Gated Linux writer; no caller return until this exact child is dead.

    The owner stays pending if kernel IO cannot acknowledge cancellation.
    Normal signals are masked only across fork/handle setup and cleanup, then
    restored inside the protected parent scope; SIGKILL remains unmaskable.
    """
    import signal
    import time
    _memory_need(type(timeout) in (int,float) and math.isfinite(timeout) and 0 < timeout <= 10,
                 'headroom_write_budget_invalid')
    _memory_need(hasattr(os,'pidfd_open') and hasattr(signal,'pidfd_send_signal')
                 and signal.getsignal(signal.SIGCHLD)==signal.SIG_DFL,
                 'headroom_exact_child_control_unsupported')
    cancellation={signal.SIGINT,signal.SIGTERM,signal.SIGHUP,signal.SIGALRM,signal.SIGQUIT}
    old_mask=signal.pthread_sigmask(signal.SIG_BLOCK,cancellation)
    gate_r=gate_w=child=writer_pidfd=None
    reaped=False
    try:
        gate_r,gate_w=os.pipe()
        child=os.fork()
        if child==0:
            try:
                os.close(gate_w)
                allowed=os.read(gate_r,1)==b'1'
                if allowed:
                    signal.pthread_sigmask(signal.SIG_SETMASK,old_mask)
                    if command is not None:
                        quiet=os.open(os.devnull,os.O_RDWR)
                        for descriptor in (0,1,2):os.dup2(quiet,descriptor)
                        if quiet>2:os.close(quiet)
                        os.execv(command[0],command)
                    if os.write(fd,payload)!=len(payload):os._exit(71)
                os._exit(0 if allowed else 72)
            except BaseException:os._exit(70)
        os.close(gate_r);gate_r=None
        # The gated child cannot exit normally before its exact pidfd opens.
        # Supported callers have default SIGCHLD and no global child reaper.
        writer_pidfd=os.pidfd_open(child,0)
        ticks=(Path('/proc')/str(child)/'stat').read_text().rsplit(')',1)[1].split()[19]
        record_writer({'pid':child,'start_ticks':ticks})
        signal.pthread_sigmask(signal.SIG_SETMASK,old_mask)
        _memory_need(os.write(gate_w,b'1')==1,'headroom_writer_gate_failed')
        os.close(gate_w);gate_w=None
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            got,status=os.waitpid(child,os.WNOHANG)
            if got:
                reaped=True
                _memory_need(os.WIFEXITED(status) and os.WEXITSTATUS(status)==0,
                             'headroom_writer_failed')
                return
            time.sleep(min(0.02,max(0,deadline-time.monotonic())))
        signal.pidfd_send_signal(writer_pidfd,signal.SIGKILL,None,0)
        # A timeout never proves the scalar unchanged. Cleanup must acknowledge
        # exit, including a pathological D-state, before releasing the owner.
        while not reaped:
            try:os.waitpid(child,0);reaped=True
            except ChildProcessError:reaped=True
            except BaseException:continue
        raise RuntimeError('headroom_write_timed_out_child_reaped')
    finally:
        complete=False
        while not complete:
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK,cancellation)
                if gate_w is not None:
                    try:os.close(gate_w)
                    except OSError as error:
                        if error.errno!=9:raise
                    gate_w=None
                if child is not None and child>0 and not reaped:
                    got,status=os.waitpid(child,os.WNOHANG)
                    if not got:
                        if writer_pidfd is not None:
                            signal.pidfd_send_signal(writer_pidfd,signal.SIGKILL,None,0)
                        # Without a pidfd, the closed unopened gate permits
                        # only child exit, never a scalar write.
                        os.waitpid(child,0)
                    reaped=True
                if gate_r is not None:
                    try:os.close(gate_r)
                    except OSError as error:
                        if error.errno!=9:raise
                    gate_r=None
                if writer_pidfd is not None:
                    try:os.close(writer_pidfd)
                    except OSError as error:
                        if error.errno!=9:raise
                    writer_pidfd=None
                complete=True
            except ChildProcessError:reaped=True
            except BaseException:continue
        signal.pthread_sigmask(signal.SIG_SETMASK,old_mask)


def _memory_high_definition(raw, current):
    """The generated drop-in may own exactly MemoryHigh, never other policy."""
    try:text=raw.decode('utf-8')
    except UnicodeDecodeError:raise RuntimeError('headroom_systemd_high_definition_unqualified')from None
    directives=[]
    for line in text.splitlines():
        line=line.strip()
        if line and not line.startswith(('#',';')):directives.append(line)
    _memory_need(directives==['[Scope]','MemoryHigh='+current],
                 'headroom_systemd_high_definition_unqualified')


def _memory_scope_read(unit, relative, timeout):
    """Read the qualified systemd owner; unsupported managers never fall back."""
    import subprocess
    _memory_need(re.fullmatch(r'docker-[0-9a-f]{64}\.scope',unit)is not None
                 and relative=='/system.slice/'+unit,'headroom_systemd_scope_invalid')
    properties='Id,LoadState,ActiveState,Transient,ControlGroup,MemoryHigh,MemoryMax,MemorySwapMax,FragmentPath,DropInPaths'
    result=subprocess.run(['/usr/bin/systemctl','show',unit,'--property='+properties],
                          stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          timeout=timeout,check=False)
    _memory_need(result.returncode==0 and len(result.stdout)<=16384,'headroom_systemd_unavailable')
    rows=result.stdout.decode('utf-8').splitlines();value=dict(row.split('=',1)for row in rows if'='in row)
    _memory_need(len(rows)==len(value)==10 and value.get('Id')==unit
        and value.get('LoadState')=='loaded'and value.get('ActiveState')=='active'
        and value.get('Transient')=='yes'and value.get('ControlGroup')==relative
        and value.get('MemoryMax')==str(MAX_BYTES)and value.get('MemorySwapMax')==str(SWAP_BYTES),
        'headroom_systemd_owner_or_caps_changed')
    high=value['MemoryHigh']
    _memory_need(high=='infinity'or(high.isdecimal()and str(int(high))==high),
                 'headroom_systemd_high_unresolved')
    # Preserve every existing non-MemoryHigh definition. The one property owned
    # here may gain its normal systemctl-generated runtime drop-in.
    paths=[value['FragmentPath'],*value['DropInPaths'].split()];other={}
    for name in paths:
        fragment='/run/systemd/transient/'+unit
        _memory_need(name==fragment or name.startswith(fragment+'.d/50-')
                     and re.fullmatch(r'[A-Za-z0-9_-]+\.conf',Path(name).name)is not None,
                     'headroom_systemd_definition_unqualified')
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        with os.fdopen(descriptor,'rb')as stream:
            info=os.fstat(stream.fileno())
            _memory_need(stat.S_ISREG(info.st_mode)and info.st_uid==0 and not info.st_mode&0o022
                         and info.st_size<=16384,'headroom_systemd_definition_unsafe')
            raw=stream.read(16385);after=os.fstat(stream.fileno())
            _memory_need(len(raw)<=16384 and (info.st_dev,info.st_ino,info.st_mtime_ns,info.st_size)==
                         (after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size),
                         'headroom_systemd_definition_raced')
        if Path(name).name=='50-MemoryHigh.conf':
            _memory_high_definition(raw,high)
        else:
            other[name]={'sha256':hashlib.sha256(raw).hexdigest(),'mode':stat.S_IMODE(info.st_mode)}
    return {'unit':unit,'control_group':relative,'memory_high':high,'other_definitions':other}


def _memory_host_boot_id(proc_root):
    value=(Path(proc_root)/'sys/kernel/random/boot_id').read_text().strip()
    _memory_need(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value)is not None,
                 'headroom_generation_boot_unknown')
    return value


def _memory_generation_owner(manager,before,relative,boot):
    return {'instance_id':manager.context.instance_id,'container_id':before[0],
            'image_id':before[1],'pid':before[2],'started_at':before[3],
            'start_ticks':before[4],'unit':'docker-'+before[0]+'.scope',
            'control_group':relative,'host_boot_id':boot}


def _memory_generation_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _memory_generation_leaf(path,maximum=32768):
    document=_private_json(path,maximum=maximum)
    if document is None:return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd);raw=os.read(fd,maximum+1);after=os.fstat(fd)
        identity=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        _memory_need(stat.S_ISREG(info.st_mode)and info.st_uid==os.geteuid()
            and not info.st_mode&0o077 and info.st_nlink==1 and 0<len(raw)<=maximum
            and identity==[after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns]
            and json.loads(raw)==document,'headroom_generation_leaf_changed')
        return {'document':document,'sha256':hashlib.sha256(raw).hexdigest(),'identity':identity}
    finally:os.close(fd)


def _memory_generation_directory(path):
    path.mkdir(mode=0o700,exist_ok=True)
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    info=os.fstat(descriptor)
    if info.st_uid!=os.geteuid()or stat.S_IMODE(info.st_mode)!=0o700:
        os.close(descriptor);raise RuntimeError('headroom_generation_directory_unsafe')
    return descriptor


def _memory_generation_existing_directories(root,parts):
    """Fence existing private parents without following a directory symlink."""
    descriptors=[]
    try:
        try:descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        except FileNotFoundError:return False
        descriptors.append(descriptor)
        for component in [None,*parts]:
            if component is not None:
                try:descriptor=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=descriptors[-1])
                except FileNotFoundError:return False
                descriptors.append(descriptor)
            info=os.fstat(descriptor)
            _memory_need(info.st_uid==os.geteuid()and stat.S_IMODE(info.st_mode)==0o700,
                         'headroom_generation_directory_unsafe')
        return True
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def _memory_generation_noreplace(source_fd,source_name,target_fd,target_name):
    # Both directories are held open and private. The existing instance lock
    # serializes cooperating writers. Post-move inode/hash proof detects an
    # external administrator replacement; no unknown file is deleted.
    import ctypes
    _memory_need(sys.platform=='linux','headroom_generation_noreplace_unsupported')
    libc=ctypes.CDLL(None,use_errno=True)
    operation=getattr(libc,'renameat2',None)
    _memory_need(operation is not None,'headroom_generation_noreplace_unsupported')
    operation.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    operation.restype=ctypes.c_int
    if operation(source_fd,os.fsencode(source_name),target_fd,os.fsencode(target_name),1)!=0:
        number=ctypes.get_errno();raise OSError(number,os.strerror(number))


def _memory_generation_old_gone(old,proc_root,current_boot):
    _memory_need(type(old.get('pid'))is int and old['pid']>1
                 and isinstance(old.get('start_ticks'),str)and old['start_ticks'].isdecimal(),
                 'headroom_generation_old_identity_invalid')
    # An exact recorded prior host boot is a stronger death proof than a PID
    # number which may already belong to an unrelated process after reboot.
    if old['host_boot_id']!=current_boot:return
    try:
        fields=(Path(proc_root)/str(old['pid'])/'stat').read_text().rsplit(')',1)[1].split()
        _memory_need(len(fields)>19 and fields[19].isdecimal(),'headroom_generation_old_identity_unknown')
    except FileNotFoundError:return
    except (IndexError,PermissionError):
        raise RuntimeError('headroom_generation_old_identity_unknown')from None
    _memory_need(fields[19]!=old['start_ticks'],'headroom_generation_old_init_still_present')


def _memory_generation_transition(manager,cid,before,relative,snapshot,state_root,proc_root,timeout,operation):
    """Recover a concrete same-CID generation archive before normal apply.

    A per-new-generation durable intent fences the absent-active-ledger crash
    window. Pending old setter acknowledgements are never archived or adopted.
    Legacy scalar receipts remain byte-for-byte outside this transaction.
    """
    boot=_memory_host_boot_id(proc_root);owner=_memory_generation_owner(manager,before,relative,boot)
    generation=_memory_generation_digest(owner)
    transitions=state_root/(cid+'.generation-transitions')
    transition_path=transitions/(generation+'.json')
    index_path=state_root/(cid+'.generation-active.json')
    active=state_root/(cid+'.systemd.json')
    _memory_generation_existing_directories(state_root,[])
    _memory_generation_existing_directories(state_root,[transitions.name])
    intent=_private_json(transition_path,maximum=32768)
    current=_memory_generation_leaf(active)
    index=_private_json(index_path,maximum=4096)
    if index is not None:
        indexed=index.get('new_owner')
        _memory_need(index.get('schema')=='dev.xenoid.memory-high-generation-index/v1'
            and isinstance(indexed,dict)and index.get('generation')==_memory_generation_digest(indexed),
            'headroom_generation_index_invalid')
        indexed_intent=_private_json(transitions/(index['generation']+'.json'),maximum=32768)
        _memory_need(indexed_intent is not None and indexed_intent.get('new_owner')==indexed,
                     'headroom_generation_index_owner_missing')
        if index['generation']!=generation:
            _memory_need(indexed_intent.get('phase')=='archive_committed'
                and current is not None and all(current['document'].get(k)==v for k,v in indexed.items()),
                'headroom_generation_prior_transition_pending_or_unknown')
            prior_old=indexed_intent.get('old_owner');prior_leaf=indexed_intent.get('old_ledger')
            _memory_need(isinstance(prior_old,dict)and isinstance(prior_leaf,dict)
                and re.fullmatch('[0-9a-f]{64}',str(prior_leaf.get('sha256','')))is not None,
                'headroom_generation_prior_archive_invalid')
            prior_parts=['generation-archive',cid,_memory_generation_digest(prior_old),prior_leaf['sha256']+'.json']
            _memory_generation_existing_directories(state_root,prior_parts[:-1])
            _memory_need(indexed_intent.get('archive_parts')==prior_parts
                and _memory_generation_leaf(state_root.joinpath(*prior_parts))==prior_leaf,
                'headroom_generation_prior_archive_changed')
    if intent is None and current is None:return None
    if intent is None:
        old=current['document']
        # Leave all same-generation validation and pending ACK reconciliation
        # to the ordinary scope path. It may enrich a committed v1 owner only
        # after exact current unit+kernel readback has succeeded.
        same_runtime=all(old.get(k)==v for k,v in owner.items()if k!='host_boot_id')
        if same_runtime and ('host_boot_id'not in old or old['host_boot_id']==boot):
            return None
        static=('instance_id','container_id','image_id','unit','control_group')
        _memory_need(old.get('schema')=='dev.xenoid.systemd-memory-high-owner/v1'
            and all(old.get(k)==owner[k]for k in static),'headroom_generation_foreign_owner')
        _memory_need(old.get('phase')in('applied','restored')and 'writer'not in old,
                     'headroom_generation_pending_old_ack')
        desired=old.get('desired_raw_high')
        _memory_need(old.get('original_unit_high')=='infinity'and old.get('original_raw_high')=='max'
            and isinstance(desired,str)and (desired=='max'or(desired.isdecimal()
                and MAX_BYTES-1024**3<=int(desired)<=MAX_BYTES))
            and old.get('desired_unit_high')==('infinity'if desired=='max'else desired)
            and (old['phase']=='restored')==(desired=='max'),
            'headroom_generation_old_ledger_invalid')
        _memory_need(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                     str(old.get('host_boot_id','')))is not None,
                     'headroom_generation_old_boot_unknown')
        if old['host_boot_id']!=boot:
            _memory_need(snapshot['memory.high']=='max','headroom_generation_new_boot_raw_not_pristine')
        _memory_generation_old_gone(old,proc_root,boot)
        old_owner={k:old[k]for k in owner}
        old_generation=_memory_generation_digest(old_owner)
        legacy=_memory_generation_leaf(state_root/(cid+'.json'),16384)
        _memory_need(legacy is None or legacy['document'].get('phase')=='applied',
                     'headroom_generation_legacy_pending')
        scope=_memory_scope_read(owner['unit'],relative,timeout)
        _memory_need(scope['memory_high']=='infinity','headroom_generation_unowned_new_unit_high')
        _memory_need(operation!='inspect','headroom_generation_transition_requires_apply')
        archive_parts=['generation-archive',cid,old_generation,current['sha256']+'.json']
        intent={'schema':'dev.xenoid.memory-high-generation-transition/v1','new_owner':owner,
                'old_owner':old_owner,'old_ledger':current,'legacy_scalar':legacy,
                'archive_parts':archive_parts,'new_other_definitions':scope['other_definitions'],
                'phase':'pending_archive'}
        _memory_record(transition_path,intent)
    _memory_need(intent.get('schema')=='dev.xenoid.memory-high-generation-transition/v1'
        and intent.get('new_owner')==owner and intent.get('phase')in('pending_archive','archive_committed'),
        'headroom_generation_transition_owner_changed')
    old=intent['old_owner'];old_leaf=intent['old_ledger']
    expected_parts=['generation-archive',cid,_memory_generation_digest(old),old_leaf['sha256']+'.json']
    _memory_need(intent.get('archive_parts')==expected_parts
        and old_leaf['document'].get('phase')in('applied','restored')
        and 'writer'not in old_leaf['document']
        and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',str(old.get('host_boot_id','')))is not None
        and all(old_leaf['document'].get(k)==v for k,v in old.items()),
        'headroom_generation_transition_invalid')
    archive=state_root.joinpath(*expected_parts)
    _memory_generation_existing_directories(state_root,expected_parts[:-1])
    _memory_need(_memory_binding(manager,cid,proc_root)[0]==before
        and _memory_host_boot_id(proc_root)==boot,'headroom_generation_current_owner_changed')
    wanted_index={'schema':'dev.xenoid.memory-high-generation-index/v1',
                  'generation':generation,'new_owner':owner}
    if index!=wanted_index and operation!='inspect':_memory_record(index_path,wanted_index)
    if intent['phase']=='pending_archive':
        _memory_need(operation!='inspect','headroom_generation_transition_requires_apply')
        _memory_generation_old_gone(old,proc_root,boot)
        if old['host_boot_id']!=boot:
            _memory_need(snapshot['memory.high']=='max','headroom_generation_new_boot_raw_not_pristine')
        legacy=_memory_generation_leaf(state_root/(cid+'.json'),16384)
        _memory_need(legacy==intent['legacy_scalar'],'headroom_generation_legacy_changed')
        scope=_memory_scope_read(owner['unit'],relative,timeout)
        _memory_need(scope['memory_high']=='infinity'
            and scope['other_definitions']==intent['new_other_definitions'],
            'headroom_generation_new_unit_changed')
        archived=_memory_generation_leaf(archive)
        if current is not None:
            _memory_need(current==old_leaf and archived is None,'headroom_generation_archive_conflict')
            descriptors=[]
            try:
                descriptors.append(_memory_generation_directory(state_root))
                folder=state_root
                for component in expected_parts[:-1]:
                    folder=folder/component
                    descriptors.append(_memory_generation_directory(folder))
                # Recheck the leaf after all directory setup, before one move.
                _memory_need(_memory_generation_leaf(active)==old_leaf,'headroom_generation_active_replaced')
                _memory_generation_noreplace(descriptors[0],active.name,descriptors[-1],archive.name)
                os.fsync(descriptors[-1]);os.fsync(descriptors[0])
            finally:
                for descriptor in reversed(descriptors):os.close(descriptor)
            archived=_memory_generation_leaf(archive)
        _memory_need(archived==old_leaf and _memory_generation_leaf(active)is None,
                     'headroom_generation_archive_unknown')
        # Also fence a retry after rename succeeded but its caller failed before
        # either parent-directory fsync or before the commit receipt.
        for folder in (archive.parent,state_root):
            descriptor=_memory_generation_directory(folder)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        _memory_need(_memory_binding(manager,cid,proc_root)[0]==before
            and _memory_host_boot_id(proc_root)==boot,'headroom_generation_owner_changed_after_archive')
        intent={**intent,'phase':'archive_committed'}
        _memory_record(transition_path,intent)
    _memory_need(_memory_generation_leaf(archive)==old_leaf,'headroom_generation_archive_changed')
    # Normal ensure is allowed to own only a new ledger for this exact tuple.
    current=_memory_generation_leaf(active)
    _memory_need(current is None or all(current['document'].get(k)==v for k,v in owner.items()),
                 'headroom_generation_active_owner_unknown')
    return {'generation':generation,'archive_sha256':old_leaf['sha256'],'phase':intent['phase']}


def _memory_scope_sync(manager,cid,before,relative,snapshot,target,read_high,state_root,timeout,operation,proc_root,scalar_receipt,reserve_mib,scalar_previous):
    """Own systemd MemoryHigh separately from the legacy scalar receipt.

    A killed setter is an unknown PID1 acknowledgement, not a cancelled D-Bus
    operation. A pending owner is only reconciled by observed unit AND kernel
    target, never by sending the command again. Rollback/disabled policy restore
    the original unit property and the legacy original raw value (max), even
    when the first observation was an already-owned, smaller raw value.
    """
    unit='docker-'+cid+'.scope';path=state_root/(cid+'.systemd.json')
    current=_memory_scope_read(unit,relative,timeout)
    previous=_private_json(path,maximum=32768)
    owner={'schema':'dev.xenoid.systemd-memory-high-owner/v1','instance_id':manager.context.instance_id,
           'container_id':cid,'image_id':before[1],'pid':before[2],'started_at':before[3],
           'start_ticks':before[4],'unit':unit,'control_group':relative}
    boot=_memory_host_boot_id(proc_root)
    if previous is not None:
        _memory_need(all(previous.get(k)==v for k,v in owner.items())
            and previous.get('original_unit_high')=='infinity'and previous.get('original_raw_high')=='max'
            and previous.get('phase')in('pending','applied','restored')
            and previous.get('other_definitions')==current['other_definitions']
            and isinstance(previous.get('desired_raw_high'),str)
            and (previous['desired_raw_high']=='max'or(previous['desired_raw_high'].isdecimal()
                 and MAX_BYTES-1024*1024*1024<=int(previous['desired_raw_high'])<=MAX_BYTES))
            and previous.get('desired_unit_high')==('infinity'if previous['desired_raw_high']=='max'else previous['desired_raw_high']),
            'headroom_systemd_receipt_binding_changed')
        writer=previous.get('writer')
        if writer is not None:
            try:
                raw=(Path('/proc')/str(writer['pid'])/'stat').read_text().rsplit(')',1)[1].split()
                alive=raw[19]==writer['start_ticks']and raw[0]!='Z'
            except FileNotFoundError:alive=False
            _memory_need(not alive,'headroom_systemd_setter_still_pending')
        if previous['phase']=='pending':
            wanted=previous['desired_unit_high'];raw_wanted=previous['desired_raw_high']
            _memory_need(current['memory_high']==wanted and read_high()==raw_wanted,
                         'headroom_systemd_pending_ack_unknown')
            previous={**previous,'phase':'restored'if wanted=='infinity'else'applied'}
            previous.pop('writer',None)
            if operation!='inspect':_memory_record(path,previous)
        permitted={previous['original_unit_high'],previous['desired_unit_high']}
    else:
        permitted={'infinity'}
    _memory_need(current['memory_high']in permitted,'headroom_systemd_existing_admin_boundary')
    if operation=='inspect':
        return {'backend':'systemd','unit':unit,'memoryHigh':current['memory_high'],'changed':False}
    desired='infinity'if target=='max'else target
    changed=current['memory_high']!=desired or read_high()!=target
    if changed:
        intent={**owner,'host_boot_id':boot,'original_unit_high':'infinity','original_raw_high':'max',
                'observed_raw_before':snapshot['memory.high'],
                'previous_unit_high':current['memory_high'],'previous_raw_high':read_high(),
                'desired_unit_high':desired,'desired_raw_high':target,
                'other_definitions':current['other_definitions'],'phase':'pending'}
        _memory_need(_memory_binding(manager,cid,proc_root)[0]==before,
                     'headroom_owner_changed_before_systemd_write')
        # Intent is durable before the gated child can contact PID1. A failure
        # leaves this separate owner pending; the old scalar owner is retained.
        if scalar_previous is None or scalar_previous.get('desired_high')!=target:
            _memory_record(scalar_receipt,{'schema':'dev.xenoid.memory-headroom-owner/v1',
                'instance_id':manager.context.instance_id,'container_id':cid,
                'image_id':before[1],'original_high':'max','desired_high':target,
                'reserve_mib':reserve_mib if operation=='apply'else 0,
                'previous_high':snapshot['memory.high'],'phase':'pending'})
        _memory_record(path,intent)
        _memory_write(-1,b'',lambda writer:_memory_record(path,{**intent,'writer':writer}),timeout,
                      command=('/usr/bin/systemctl','set-property','--runtime',unit,'MemoryHigh='+desired))
    after=_memory_scope_read(unit,relative,timeout)
    _memory_need(after['other_definitions']==current['other_definitions']
                 and after['memory_high']==desired and read_high()==target,
                 'headroom_systemd_readback_failed')
    _memory_need(_memory_binding(manager,cid,proc_root)[0]==before,
                 'headroom_owner_changed_after_systemd_write')
    _memory_need(_memory_host_boot_id(proc_root)==boot,'headroom_generation_host_boot_changed_during_apply')
    if changed:
        finished={**intent,'phase':'restored'if desired=='infinity'else'applied'}
        _memory_record(path,finished)
    elif previous is not None and 'host_boot_id'not in previous and operation!='inspect':
        # Migration metadata only after the existing exact generation, unit,
        # kernel target and all non-owned definitions have been qualified.
        _memory_record(path,{**previous,'host_boot_id':boot})
    elif previous is None and operation!='inspect':
        # A new generation may already be at original infinity/max while a
        # rollback/disabled policy needs no setter. Retain its committed owner
        # so the next generation can qualify this completed transition.
        _memory_record(path,{**owner,'host_boot_id':boot,'original_unit_high':'infinity',
            'original_raw_high':'max','observed_raw_before':snapshot['memory.high'],
            'previous_unit_high':current['memory_high'],'previous_raw_high':read_high(),
            'desired_unit_high':desired,'desired_raw_high':target,
            'other_definitions':current['other_definitions'],
            'phase':'restored'if desired=='infinity'else'applied'})
    return {'backend':'systemd','unit':unit,'memoryHigh':desired,'changed':changed,
            'acknowledgement':'verified_unit_and_kernel','fallback':'none'}


def ensure_memory_headroom(manager,cid,*,reserve_mib,operation='apply',proc_root=Path('/proc'),cgroup_root=Path('/sys/fs/cgroup'),state_root=None,timeout=5):
    """Inspect, explicitly apply, or revert only memory.high on an exact owner.

    Retain the returned pre-value in the external canary receipt. Rollback
    accepts only this candidate's exact high value and restores max; it never
    rewrites hard caps. A changed preexisting admin high value is a conflict.
    The qualified systemd scope owns the same scalar; direct scalar fallback
    is intentionally unavailable. The short setter deadline reaps its child,
    but a lost PID1 acknowledgement remains pending until unit+kernel readback
    reconciles it. No cancellation claim is made about an accepted D-Bus call.
    """
    _memory_need(operation in('inspect','apply','rollback'),'headroom_operation_invalid')
    _memory_need(type(timeout)in(int,float)and math.isfinite(timeout)and 0<timeout<=10,
                 'headroom_write_budget_invalid')
    _memory_need(type(reserve_mib) is int and 0 <= reserve_mib <= 1024, 'headroom_reserve_invalid')
    desired_high = 'max' if reserve_mib == 0 else str(MAX_BYTES-reserve_mib*1024*1024)
    state_root = (STATE/'memory-headroom') if state_root is None else Path(state_root)
    _memory_need(re.fullmatch('[0-9a-f]{64}',cid) is not None,'full_CID_required')
    if (reserve_mib==0 and operation!='rollback'
            and _private_json(state_root/(cid+'.json'),maximum=16384) is None
            and _private_json(state_root/(cid+'.systemd.json'),maximum=32768) is None
            and not (state_root/(cid+'.generation-transitions')).exists()):
        # Disabled policy never imposes this candidate's cap/cgroup contract
        # on an administrator's unrelated runtime or claims its high value.
        before,_=_memory_binding(manager,cid,Path(proc_root),qualified=False)
        _memory_need(_memory_binding(manager,cid,Path(proc_root),qualified=False)[0]==before,
                     'headroom_disabled_owner_changed')
        return {'ok':True,'changed':False,'skipped':'disabled_no_owned_receipt',
                'containerId':cid,'hardCapsChanged':False,'cgroupWrites':0}
    before,relative=_memory_binding(manager,cid,Path(proc_root))
    folder=Path(cgroup_root)/relative.lstrip('/')
    fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        ds=os.fstat(fd);_memory_need(stat.S_ISDIR(ds.st_mode) and _memory_filesystem_type(fd)==0x63677270,'headroom_cgroup_directory_invalid')
        def read(name):
            f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            try:
                s=os.fstat(f);_memory_need(stat.S_ISREG(s.st_mode) and s.st_dev==ds.st_dev and _memory_filesystem_type(f)==0x63677270,'headroom_cgroup_file_invalid')
                raw=os.read(f,65537);_memory_need(len(raw)<=65536,'headroom_cgroup_file_oversize');return raw.decode().strip()
            finally:os.close(f)
        snapshot={n:read(n)for n in('memory.current','memory.high','memory.max','memory.swap.max','memory.events','memory.pressure','cpu.max')}
        _memory_need(snapshot['memory.max']==str(MAX_BYTES)and snapshot['memory.swap.max']==str(SWAP_BYTES),
             'headroom_cgroup_caps_changed')
        cpu=snapshot['cpu.max'].split();_memory_need(len(cpu)==2 and cpu[0].isdigit()and cpu[1].isdigit()
             and int(cpu[0])==int(cpu[1])and int(cpu[1])>0,'headroom_cpu_cap_changed')
        receipt=state_root/(cid+'.json')
        previous=_private_json(receipt,maximum=16384)
        if previous is not None:
            _memory_need(previous.get('schema')=='dev.xenoid.memory-headroom-owner/v1'
                and previous.get('instance_id')==manager.context.instance_id
                and previous.get('container_id')==cid and previous.get('image_id')==before[1]
                and previous.get('original_high')=='max'
                and previous.get('phase') in ('pending','applied')
                and type(previous.get('reserve_mib')) is int
                and 0 <= previous['reserve_mib'] <= 1024
                and previous.get('desired_high') == ('max' if previous['reserve_mib']==0 else str(MAX_BYTES-previous['reserve_mib']*1024*1024)), 'headroom_receipt_binding_changed')
            writer=previous.get('writer')
            if writer is not None:
                try:
                    raw=(Path('/proc')/str(writer['pid'])/'stat').read_text().rsplit(')',1)[1].split()
                    alive=raw[19]==writer['start_ticks'] and raw[0]!='Z'
                except FileNotFoundError:alive=False
                _memory_need(not alive,'headroom_prior_writer_still_pending')
            permitted={'max',str(previous['desired_high'])}
            if previous.get('phase')=='pending':
                permitted.add(str(previous.get('previous_high','max')))
        else:
            permitted={'max'}
        _memory_need(snapshot['memory.high']in permitted,'headroom_existing_admin_boundary')
        target=desired_high if operation=='apply'else'max'
        if operation=='rollback':
            _memory_need(previous is not None,'headroom_rollback_requires_original_receipt')
        _memory_need(_memory_binding(manager,cid,Path(proc_root))[0]==before,'headroom_owner_changed_before_write')
        generation=_memory_generation_transition(manager,cid,before,relative,snapshot,state_root,Path(proc_root),timeout,operation)
        systemd=_memory_scope_sync(manager,cid,before,relative,snapshot,target,lambda:read('memory.high'),
                                   state_root,timeout,operation,Path(proc_root),receipt,reserve_mib,previous)
        changed=systemd['changed']
        after_high=read('memory.high')
        _memory_need(after_high==(target if operation!='inspect'else snapshot['memory.high']),'headroom_readback_failed')
        _memory_need(read('memory.max')==snapshot['memory.max']and read('memory.swap.max')==snapshot['memory.swap.max']
             and read('cpu.max')==snapshot['cpu.max'],'headroom_hard_caps_changed')
        _memory_need(_memory_binding(manager,cid,Path(proc_root))[0]==before and (os.stat(folder,follow_symlinks=False).st_dev,os.stat(folder,follow_symlinks=False).st_ino)==(ds.st_dev,ds.st_ino),
             'headroom_owner_changed_after_read')
        if operation!='inspect':
            final_scalar={'schema':'dev.xenoid.memory-headroom-owner/v1',
                'instance_id':manager.context.instance_id,'container_id':cid,
                'image_id':before[1],'original_high':'max','desired_high':target,
                'reserve_mib':reserve_mib if operation=='apply' else 0,'phase':'applied'}
            if final_scalar!=previous:_memory_record(receipt,final_scalar)
        return {'schema':'dev.xenoid.cgroup-reclaim-headroom/v1','ok':True,'operation':operation,'changed':changed,
                'binding':{'containerId':before[0],'imageId':before[1],'pid':before[2],'StartedAt':before[3],'startTicks':before[4]},
                'cgroup':relative,'before':snapshot,'afterHigh':after_high,'hardCapsChanged':False,
                'cacheDrops':0,'processSignals':0,'guestWrites':0,'systemd':systemd}
    finally:os.close(fd)


def ensure_configured_memory_headroom(manager, expected_container_id, *, timeout=5):
    """Owning UP hook; uses the same central resource policy as the timer.

    The native caller already holds its instance operation lock. No new lock,
    SDK command, Android command, lifecycle operation or service is launched.
    """
    if not CONFIG.exists():
        return {"ok": True, "skipped": "qa_resource_policy_not_installed"}
    config = load_config()
    if not config["enabled"] or config["render_fps"] is None:
        return {"ok": True, "skipped": "memory_headroom_not_enabled"}
    return ensure_memory_headroom(manager, expected_container_id,
                                  reserve_mib=config["memory_reserve_mib"],timeout=timeout)


if __name__ == "__main__":
    raise SystemExit(main())
