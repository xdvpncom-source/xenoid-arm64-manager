"""Independent QA renderer-name settings and safe idle-only host convergence.

Only the native renderer command changes the mapping. This helper never edits
identity profiles, GEN journals, APKs, driver capabilities or native image files.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

DEFAULT_QA_GRAPHICS = {"renderer_label": "Mali-G78"}
MAX_RENDERER_LABEL_BYTES = 96
CONFIG = Path("/etc/xenoid-fleet/qa-graphics.json")
STATE = Path("/var/lib/xenoid-fleet/qa-graphics")
RUNS = Path("/var/lib/xenoid-panel-scripts")
FLEET = Path("/usr/local/bin/xenoid-fleet")
SCHEMA = "dev.xenoid.qa.graphics/v1"
_SOURCE_GENERATION_CACHE = {}
OWNER = 0
CGROUP_ROOT = Path("/sys/fs/cgroup")
JOURNALS = ("device-regenerate-v3.json", "convergence-v1.json", "device-regenerate-v2.json", "device-regenerate.json")


def validate_qa_graphics(value):
    if not isinstance(value, dict) or set(value) - {"renderer_label"}:
        raise ValueError("Provide only the reported renderer_label setting")
    label = value.get("renderer_label", DEFAULT_QA_GRAPHICS["renderer_label"])
    if label == "native":
        label = None
    if label is not None and (not isinstance(label, str) or not label or label != label.strip()
            or not label.isascii() or len(label.encode("ascii")) > MAX_RENDERER_LABEL_BYTES
            or any(ord(c) < 32 or ord(c) > 126 for c in label)):
        raise ValueError("Renderer label must be printable ASCII, at most 96 bytes, or null for native")
    return {"renderer_label": label}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError("graphics_private_state_invalid")
        return json.load(stream)


def atomic_json(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".graphics-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, sort_keys=True, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def lifecycle_gate(number, *, worker=False):
    """Workers retain a shared lock; graphics reload owns the exclusive lock."""
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("graphics_instance_invalid")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(STATE / (str(number) + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("graphics_lifecycle_lock_invalid")
        fcntl.flock(fd, fcntl.LOCK_SH if worker else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def settings_lock():
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(CONFIG.parent / ".qa-graphics.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("graphics_settings_lock_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_document():
    saved = read_private(CONFIG)
    if saved is None:
        return {"schema": SCHEMA, "settings": dict(DEFAULT_QA_GRAPHICS), "revision": 0, "pending": True}
    if (saved.get("schema") != SCHEMA or type(saved.get("revision")) is not int
            or saved["revision"] < 1 or type(saved.get("pending")) is not bool):
        raise ValueError("graphics_settings_schema_invalid")
    return {**saved, "settings": validate_qa_graphics(saved["settings"])}


def load_settings():
    return load_document()["settings"]


def configure(value):
    settings = validate_qa_graphics(value)
    with settings_lock():
        previous = load_document()
        document = {"schema": SCHEMA, "settings": settings, "revision": previous["revision"] + 1, "pending": True}
        atomic_json(CONFIG, document)
    return {"ok": True, "settings": settings, "settings_sha256": digest(settings), "revision": document["revision"]}


def acknowledge(document):
    # A concurrent panel save must remain pending for its own generation.
    with settings_lock():
        current = load_document()
        if current["revision"] == document["revision"] and current["settings"] == document["settings"]:
            atomic_json(CONFIG, {**current, "revision": max(1, current["revision"]), "pending": False})
            return True
    return False


def worker_inactive(number):
    unit = "xenoid-panel-script@%s.service" % number
    result = subprocess.run(["systemctl", "show", unit,
                             "--property=ActiveState,SubState,MainPID,ControlPID,ControlGroup"],
                            capture_output=True, text=True, timeout=5, check=False)
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not (result.returncode == 0 and (props.get("ActiveState"), props.get("SubState")) in {
            ("inactive", "dead"), ("failed", "failed")} and props.get("MainPID") == props.get("ControlPID") == "0"):
        return False
    group = props.get("ControlGroup", "")
    if group:
        if not group.startswith("/system.slice/") or not group.endswith("/" + unit) or ".." in Path(group).parts:
            return False
        root = CGROUP_ROOT / group.lstrip("/")
        if root.exists():
            if root.is_symlink():
                return False
            files = [root / "cgroup.procs", *root.rglob("*/cgroup.procs")]
            if len(files) > 4096 or any(path.read_text().strip() for path in files):
                return False
    return True


def pending_receipts(number):
    path = RUNS / str(number) / "runs.db"
    if not path.exists():
        return False
    # Read-only connection must not create a database or rewrite a receipt.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return any(json.loads(row[0]).get("status") != "committed"
                   for row in connection.execute("SELECT document FROM regeneration_receipts"))
    finally:
        connection.close()


def pending(manager, owned, number):
    from qa_resource_runtime import pending_profile
    return (any(os.path.lexists(manager.context.state_root / name) for name in JOURNALS)
            or pending_profile(manager, owned) or pending_receipts(number))


def native_document(upstream, name, arguments, value=None, *, expected_id=None):
    command = [str(upstream / "xenoid"), "--instance", name, "renderer", *arguments]
    kwargs = {"input": json.dumps(value) + "\n"} if value is not None else {"stdin": subprocess.DEVNULL}
    environment = dict(os.environ, XENOID_PROJECT=str(upstream))
    environment.pop("XENOID_INSTANCE", None)
    environment.pop("XENOID_EXPECT_INSTANCE_ID", None)
    if expected_id is not None:
        environment["XENOID_EXPECT_INSTANCE_ID"] = expected_id
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=240,
                            env=environment, **kwargs)
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError("graphics_native_response_invalid")
    return document, result.returncode


def proof(status, settings):
    epoch = status.get("runtime") or {}
    wanted = settings["renderer_label"]
    return (status.get("ok") is True and status.get("schema") == "dev.xenoid.renderer/v1"
            and status.get("reportedLabel") == wanted and status.get("mapped") is True
            and isinstance(epoch.get("containerId"), str) and type(epoch.get("initPid")) is int
            and epoch["initPid"] > 0 and bool(epoch.get("startedAt"))
            and (status.get("state") == "native" if wanted is None else
                 status.get("state") == "bound" and status.get("owned") is True
                 and status.get("readonly") is True and type(status.get("zygoteCount")) is int
                 and status["zygoteCount"] > 0 and bool(status.get("sourceSha256"))))



def source_generation(upstream):
    paths = ["src/xenoid/" + name + ".py" for name in
             ("graphics", "artifacts", "backend", "convergence", "live_observe", "config", "cli")]
    paths += ["scripts/profile-gles-renderer.py", "scripts/renderer-platform-library.py",
              "scripts/build-gles-renderer.sh", "native/xenoid-renderer/xenoid_renderer.c",
              "scripts/build-native-renderer.sh", "scripts/build-native-for-arch.sh", "scripts/android-sdk-root.sh"]
    files = [upstream / name for name in paths] + [Path(__file__)]
    facts = tuple((str(path), path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) for path in files)
    if facts not in _SOURCE_GENERATION_CACHE:
        _SOURCE_GENERATION_CACHE.clear()
        _SOURCE_GENERATION_CACHE[facts] = digest({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files})
    return _SOURCE_GENERATION_CACHE[facts]


def reconcile_one(name, settings, *, inspect_only=False, owned_worker=False, expected_binding=None):
    from qa_resource_runtime import native_manager, UPSTREAM
    number = name[len("phone-"):]
    def deferred(reason):
        return {"instance": name, "ok": True, "deferred": True, "reason": reason}
    try:
        if inspect_only:
            status, code = native_document(UPSTREAM, name, ["status"])
            if not code and proof(status, settings):
                return {"instance": name, "ok": True, "deferred": False, "reported_label": settings["renderer_label"], "runtime": status["runtime"]}
            return deferred("renderer_not_converged")
        if not owned_worker:
            accepted = accepted_cached(name, settings)
            if accepted is not None:
                return accepted
        with nullcontext() if owned_worker else lifecycle_gate(number):
            if not owned_worker and not worker_inactive(number):
                return deferred("qa_worker_active")
            manager = native_manager(name)
            if expected_binding is not None and any(expected_binding.get(key) != value for key, value in {
                    "instance_id": manager.context.instance_id, "resource_tag": manager.context.resource_tag}.items()):
                raise ValueError("graphics_worker_assignment_changed")
            with manager.instance_operation_lock(timeout_seconds=0):
                owned, _ = manager._owned_container_record(timeout=8)
                if owned is None or (owned.get("State") or {}).get("Running") is not True:
                    return deferred("runtime_not_running")
                if expected_binding is not None and owned["Id"] != expected_binding.get("container_id"):
                    raise ValueError("graphics_worker_container_changed")
                if pending(manager, owned, number):
                    return deferred("recorded_transaction_pending")
            status, code = native_document(UPSTREAM, name, ["status"], expected_id=manager.context.instance_id)
            if code or not proof(status, settings):
                if inspect_only:
                    return deferred("renderer_not_converged")
                # Native command owns its operation lock and all runtime changes.
                result, code = native_document(UPSTREAM, name, ["configure", "--json-stdin"], settings, expected_id=manager.context.instance_id)
                if code or result.get("ok") is not True:
                    reason = result.get("error", "graphics_native_pending")
                    if reason in {"graphics_convergence_pending", "device_regeneration_pending", "instance_busy",
                                  "device_regeneration_legacy_pending", "device_regeneration_state_invalid"}:
                        return deferred(reason)
                    raise RuntimeError("graphics_native_configure_failed")
                status, code = native_document(UPSTREAM, name, ["status"], expected_id=manager.context.instance_id)
                if code or not proof(status, settings):
                    raise RuntimeError("graphics_readback_unproven")
            if expected_binding is not None and status.get("runtime", {}).get("containerId") != expected_binding.get("container_id"):
                raise ValueError("graphics_worker_container_changed")
            # Fresh status verifies RO ownership/mapping even after cold restarts.
            # Success never calls up again; runtime epoch is part of the marker.
            marker = {"schema": SCHEMA, "settings_sha256": digest(settings),
                      "source_generation": source_generation(UPSTREAM),
                      "runtime": status["runtime"], "source_sha256": status.get("sourceSha256"),
                      "renderer_proof_sha256": digest(status),
                      "instance_id": manager.context.instance_id, "resource_tag": manager.context.resource_tag}
            atomic_json(STATE / (number + ".json"), marker)
            return {"instance": name, "ok": True, "deferred": False,
                    "reported_label": settings["renderer_label"], "runtime": status["runtime"]}
    except BlockingIOError:
        return deferred("qa_worker_or_graphics_busy")
    except Exception as error:
        if getattr(error, "code", None) == "instance_busy":
            return deferred("native_operation_busy")
        # Do not copy raw native errors, credentials or URLs into panel logs.
        return {"instance": name, "ok": False, "code": "graphics_reconcile_unproven"}



def worker_owner(number, config, manager):
    unit = "xenoid-panel-script@%s.service" % number
    result = subprocess.run(["systemctl", "show", unit,
                             "--property=ActiveState,SubState,MainPID,ControlPID,ControlGroup"],
                            capture_output=True, text=True, timeout=5, check=False)
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    group = props.get("ControlGroup", "")
    if (os.geteuid() != OWNER or result.returncode or props.get("ActiveState") != "active"
            or props.get("SubState") != "running" or props.get("MainPID") != str(os.getpid())
            or props.get("ControlPID") != "0" or not group.startswith("/system.slice/")
            or not group.endswith("/" + unit) or ".." in Path(group).parts
            or ("0::" + group) not in Path("/proc/self/cgroup").read_text().splitlines()
            or config.get("instance_id") is not None and config["instance_id"] != manager.context.instance_id
            or config.get("resource_tag") != manager.context.resource_tag):
        raise ValueError("graphics_worker_owner_unproven")
    root = CGROUP_ROOT / group.lstrip("/")
    files = [root / "cgroup.procs", *root.rglob("*/cgroup.procs")]
    if root.is_symlink() or len(files) > 4096 or any(
            set(path.read_text().split()) - {str(os.getpid())} for path in files):
        raise ValueError("graphics_worker_children_active")
    connection = sqlite3.connect((RUNS / str(number) / "runs.db").as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        current = connection.execute("SELECT id,config FROM runs WHERE current=1").fetchone()
        if current is None or current[0] != config.get("run_id") or json.loads(current[1]) != config:
            raise ValueError("graphics_worker_assignment_changed")
    finally:
        connection.close()


def process_start_ticks(pid):
    if type(pid) is not int or pid < 1:
        return None
    line = Path("/proc/%d/stat" % pid).read_text()
    return int(line.rsplit(")", 1)[1].split()[19])



def cached_proof(number, settings, manager, owned):
    marker = read_private(STATE / (str(number) + ".json")) or {}
    epoch = marker.get("runtime") or {}
    state = owned.get("State") or {}
    return (marker.get("instance_id") == manager.context.instance_id
            and marker.get("resource_tag") == manager.context.resource_tag
            and marker.get("settings_sha256") == digest(settings)
            and marker.get("source_generation") == source_generation(manager.context.project_root)
            and epoch.get("containerId") == owned["Id"]
            and epoch.get("initPid") == state.get("Pid")
            and epoch.get("startedAt") == state.get("StartedAt")
            and epoch.get("initStartTicks") == process_start_ticks(state.get("Pid"))
            and bool(marker.get("renderer_proof_sha256")))


def accepted_cached(name, settings):
    """Read-only proof cache lets idle timer acknowledge an active worker's work."""
    from qa_resource_runtime import native_manager
    manager = native_manager(name)
    number = name[len("phone-"):]
    with manager.instance_operation_lock(timeout_seconds=0):
        owned, _ = manager._owned_container_record(timeout=8)
        if (owned is not None and (owned.get("State") or {}).get("Running") is True
                and not pending(manager, owned, number) and cached_proof(number, settings, manager, owned)):
            return {"instance": name, "ok": True, "deferred": False, "cached_runtime_epoch": True,
                    "reported_label": settings["renderer_label"]}
    return None


def worker_boundary(number, config, manager, gate_fd):
    """Quiescent worker boundary, after receipt recovery and before app actions."""
    try:
        worker_owner(number, config, manager)
        try:
            fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(gate_fd, fcntl.LOCK_SH)
            return {"ok": True, "deferred": True, "reason": "graphics_boundary_busy"}
        try:
            settings = load_settings()
            with manager.instance_operation_lock(timeout_seconds=0):
                owned, _ = manager._owned_container_record(timeout=8)
                if owned is None or (owned.get("State") or {}).get("Running") is not True or pending(manager, owned, number):
                    return {"ok": True, "deferred": True, "reason": "recorded_transaction_or_runtime_pending"}
                expected_cid = config.get("expected_container_id", owned["Id"])
                if re.fullmatch(r"[0-9a-f]{64}", str(expected_cid)) is None or expected_cid != owned["Id"]:
                    return {"ok": False, "code": "graphics_worker_container_changed"}
                if cached_proof(number, settings, manager, owned):
                    return {"ok": True, "deferred": False, "cached_runtime_epoch": True}
            # This worker owns EX and has not launched its app iteration yet.
            return reconcile_one("phone-" + str(number), settings, owned_worker=True,
                                 expected_binding={"instance_id": manager.context.instance_id, "resource_tag": manager.context.resource_tag,
                                                   "container_id": expected_cid})
        finally:
            fcntl.flock(gate_fd, fcntl.LOCK_SH)
    except Exception:
        return {"ok": False, "code": "graphics_worker_boundary_unproven"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "show", "reconcile", "status", "ensure-default"))
    parser.add_argument("--json-stdin", action="store_true")
    parser.add_argument("--device", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--pending-only", action="store_true", help="idle-only scheduled reconciliation")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run host graphics management as root")
    if args.action == "configure":
        if not args.json_stdin:
            parser.error("Use --json-stdin")
        value = json.loads(sys.stdin.read(4097))
        result = configure(value)
    elif args.action in {"show", "ensure-default"}:
        with settings_lock():
            document = load_document()
            settings = document["settings"]
            if args.action == "ensure-default":
                atomic_json(CONFIG, {**document, "revision": document["revision"] + 1, "pending": True})
        result = {"ok": True, "settings": settings}
    else:
        document = load_document()
        if args.pending_only and not document["pending"]:
            print(json.dumps({"ok": True, "skipped": "no_pending_graphics", "deferred_count": 0}))
            return 0
        from qa_resource_runtime import registry_names
        settings = document["settings"]
        names = registry_names() if args.all else args.device
        if any(re.fullmatch(r"phone-[1-9][0-9]*", name) is None for name in names):
            parser.error("Invalid native instance name")
        rows = [reconcile_one(name, settings, inspect_only=args.action == "status") for name in names]
        deferred = sum(row.get("deferred") is True for row in rows)
        ok = all(row.get("ok") is True for row in rows)
        acknowledged = args.action == "reconcile" and args.all and ok and deferred == 0 and acknowledge(document)
        result = {"ok": ok, "devices": rows, "deferred_count": deferred,
                  "generation_acknowledged": bool(acknowledged),
                  "applied_count": sum(row.get("ok") is True and not row.get("deferred") for row in rows)}
    print(json.dumps(result, sort_keys=True))
    return int(result.get("ok") is not True)


if __name__ == "__main__":
    raise SystemExit(main())
