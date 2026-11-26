"""Managed GEN reservation over existing privileged SSH sync; no network service."""
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import time

OWNER = 0
PROC = Path("/proc")
RADIO_UID = 1001
RADIO_GID = 1001
MAXIMUM = 512 * 1024
REQUEST_SCHEMA = "dev.xenoid.native-identity-request/v1"
OBSERVATION_SCHEMA = "dev.xenoid.native-identity-observation/v1"
RECOVERY_SCHEMA = "dev.xenoid.native-identity-recovery/v1"
TERMINAL_HANDOFF_SCHEMA = "dev.xenoid.native-identity-terminal-handoff/v1"
SCRIPT_HOST = Path(__file__).with_name("script_host.py")
NATIVE_STATE_HOME = Path.home() / ".xenoid"
CGROUP_ROOT = Path("/sys/fs/cgroup")


class ReservationPending(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(value, code):
    if not value:
        raise ReservationPending(code)


def signed(value, field):
    return {**value, field: digest(value)}


def verified(value, field):
    require(isinstance(value, dict) and value.get(field) == digest({k: v for k, v in value.items() if k != field}), "identity_document_digest_changed")
    return value


def directory(path, create=False):
    path = Path(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and stat.S_IMODE(info.st_mode) == 0o700, "identity_directory_invalid")
    return path


def read(path):
    before = Path(path).lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and opened.st_uid == OWNER and stat.S_IMODE(opened.st_mode) == 0o600 and opened.st_nlink == 1 and opened.st_size <= MAXIMUM, "identity_file_invalid")
        raw = stream.read(MAXIMUM + 1)
        after = os.fstat(stream.fileno())
    key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    require(key(before) == key(opened) == key(after) and len(raw) == opened.st_size, "identity_file_changed")
    return json.loads(raw)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_once(path, value):
    payload = canonical(value) + b"\n"
    require(len(payload) <= MAXIMUM, "identity_document_too_large")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    sync(Path(path).parent)


def replace_owned(path, value):
    path = Path(path)
    if path.exists() or path.is_symlink():
        read(path)
    temporary = path.parent / (".identity-" + secrets.token_hex(16))
    write_once(temporary, value)
    os.replace(temporary, path)
    sync(path.parent)


def run_folder(folder, run_id, create=False):
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id), "identity_run_invalid")
    directory(folder)
    base = Path(folder) / "identity-reservations"
    if not create and not base.exists():
        return None
    directory(base, create)
    target = base / run_id
    if not create and not target.exists():
        return None
    return directory(target, create)


def location_sidecar(value, target=None):
    require(isinstance(value, dict) and set(value) == {"simEpoch", "country", "profile", "profileDigest"}, "identity_location_sidecar_invalid")
    profile = value["profile"]
    require(isinstance(profile, dict) and profile.get("schema") == "dev.xenoid.cellular-profile/v2"
            and profile.get("identityDigest") == value["profileDigest"]
            and digest({k: v for k, v in profile.items() if k != "identityDigest"}) == value["profileDigest"], "identity_location_digest_changed")
    require(isinstance(value["simEpoch"], str) and (re.fullmatch(r"[0-9a-f]{32}", value["simEpoch"]) or (target is None and value["simEpoch"] == "")) and isinstance(profile.get("sim"), dict), "identity_location_epoch_invalid")
    if target is not None:
        require(value["simEpoch"] == target["simEpoch"] and value["profileDigest"] == target["locationProfileDigest"], "identity_location_target_changed")
        if "locationTarget" in target:
            require(value == target["locationTarget"], "identity_location_target_changed")
    return value


def values_from_target(target, location):
    s = target["stable"]
    return {"android_id": s["androidId"], "serial": s["serial"], "imei": s["imei"], "imeisv": s["imeisv"],
            "boot_id": target["bootId"], "bluetooth_address": target["bluetoothAddress"].upper(),
            "hostname": target["hostname"], "device_name": target["deviceName"],
            "statfs_fsid": target["statfsFsid"], "drm_device_unique_id": target["drmDeviceUniqueId"],
            **{k: location_sidecar(location, target)["profile"]["sim"][k] for k in ("imsi", "iccid", "msisdn")}}


def read_location(manager, expected=None):
    """Read existing native epoch and exact radio payload; never stage/repair."""
    from xenoid.location import LocationStateStore, location_runtime_epoch
    from xenoid.cellular import encode_profile_v1
    state = LocationStateStore(manager.context.state_root, manager.cfg.network_dns_servers).load()
    require(state and state.get("pending") is None and isinstance(state.get("active"), dict), "identity_location_not_active")
    active = state["active"]
    entry = state["profiles"].get(active.get("country"))
    require(entry and entry["simEpoch"] == state["simEpoch"] == active["simEpoch"] and entry["profileDigest"] == active["profileDigest"], "identity_location_state_changed")
    sidecar = location_sidecar({"simEpoch": state["simEpoch"], "country": active["country"], "profile": entry["profile"], "profileDigest": entry["profileDigest"]})
    if expected is not None:
        require(sidecar == location_sidecar(expected), "identity_location_readback_changed")
    owned, _ = manager._owned_container_record(timeout=5)
    require(owned and owned.get("State", {}).get("Running") is True, "identity_runtime_not_running")
    epoch = location_runtime_epoch(owned["Id"])
    observed = manager.daemon_client(timeout=5).location_status(timeout=5)
    require(observed.get("ok") is True and observed.get("state") == "active" and observed.get("profileDigest") == sidecar["profileDigest"] and observed.get("runtimeEpoch") == epoch and active.get("lastValidatedRuntimeEpoch") == epoch, "identity_location_epoch_unverified")
    path = PROC / str(owned["State"]["Pid"]) / "root/data/vendor/radio/xenoid/profile.v1"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == RADIO_UID and info.st_gid == RADIO_GID and stat.S_IMODE(info.st_mode) == 0o640 and info.st_nlink == 1 and info.st_size <= 65536, "identity_radio_file_invalid")
        raw = stream.read(65537); after = os.fstat(stream.fileno())
        require((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size), "identity_radio_file_changed")
    require(raw == encode_profile_v1(sidecar["profile"]), "identity_radio_readback_changed")
    return sidecar


READ_IDENTITY = "\n".join([
    "settings get secure android_id", "getprop ro.serialno", "getprop persist.xenoid.radio.imei",
    "getprop persist.xenoid.radio.imeisv", "cat /proc/sys/kernel/random/boot_id",
    "settings get secure bluetooth_address", "getprop net.hostname", "settings get global device_name",
    "cat /data/local/tmp/xenoid-profile/statfs_fsid", "cat /data/local/tmp/xenoid-profile/drm_device_unique_id",
    "getprop ro.product.model", "getprop ro.product.device", "getprop ro.build.fingerprint"])


def observe(manager, expected_location=None):
    """Called under the existing native EX; reads actual published surfaces."""
    first, _ = manager._owned_container_record(timeout=5)
    require(first and first.get("State", {}).get("Running") is True, "identity_runtime_not_running")
    physical = process_binding(first)
    result = manager.docker_exec(["sh", "-c", "set -e\n" + READ_IDENTITY], timeout=30)
    require(result.get("ok") is True, "identity_readback_failed")
    lines = result.get("stdout", "").splitlines()
    require(len(lines) == 13 and all(lines), "identity_readback_incomplete")
    keys = ("android_id", "serial", "imei", "imeisv", "boot_id", "bluetooth_address", "hostname", "device_name", "statfs_fsid", "drm_device_unique_id")
    values = dict(zip(keys, lines[:10])); values["bluetooth_address"] = values["bluetooth_address"].upper()
    location = read_location(manager, expected_location)
    values.update({k: location["profile"]["sim"][k] for k in ("imsi", "iccid", "msisdn")})
    last, _ = manager._owned_container_record(timeout=5)
    require(last and last.get("Id") == first.get("Id") and last.get("State", {}).get("Pid") == first.get("State", {}).get("Pid") and last.get("State", {}).get("Running") is True, "identity_runtime_changed")
    require(process_binding(last) == physical, "identity_process_binding_changed")
    return values, dict(zip(("model", "device", "fingerprint"), lines[10:]))


def process_binding(owned):
    pid = owned.get("State", {}).get("Pid")
    cid = owned.get("Id")
    require(type(pid) is int and pid > 0 and isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{64}", cid), "identity_process_binding_invalid")
    root = PROC / str(pid)
    fields = (root / "stat").read_text().rpartition(")")[2].split()
    require(len(fields) > 19 and fields[19].isdecimal(), "identity_process_stat_invalid")
    groups = (root / "cgroup").read_text()
    paths = [line.split(":", 2)[2] for line in groups.splitlines() if line.count(":") >= 2]
    require(any(cid in path.split("/") or "docker-" + cid + ".scope" in path.split("/") for path in paths), "identity_cgroup_owner_changed")
    return {"pid": pid, "start_ticks": int(fields[19]), "cgroup_sha256": hashlib.sha256(groups.encode()).hexdigest()}


def publish_observation(folder, config, manager):
    root = run_folder(folder, config["run_id"], True)
    values, build = observe(manager)
    binding = {"device_id": int(Path(folder).name), "instance_id": manager.context.instance_id,
               "resource_tag": manager.context.resource_tag, "container_id": manager.location_runtime_container_id()}
    value = signed({"schema": OBSERVATION_SCHEMA, "binding": binding, "run_id": config["run_id"],
                    "values": values, "build": build, "captured_at": time.time()}, "proof_sha256")
    replace_owned(root / "observation.json", value)
    return value


def active_request(root, run_id):
    active = read(root / "active.json")
    require(re.fullmatch(r"[0-9a-f]{64}/attempt-[0-7]\.(reserve|finalize)\.json", active.get("path", "")), "identity_request_path_invalid")
    request = verified(read(root / active["path"]), "request_sha256")
    require(request["request_sha256"] == active["sha256"] and request["run_id"] == run_id, "identity_active_request_changed")
    return active, request


def _remaining(remaining, seconds):
    value = remaining(seconds) if callable(remaining) else seconds
    require(value is not None and value > 0, "identity_recovery_timeout")
    return value


def _recovery_worker(folder, remaining=None):
    number = int(Path(folder).name)
    require(number > 0, "identity_recovery_device_invalid")
    unit = "xenoid-panel-script@%d.service" % number
    shown = subprocess.run(["systemctl", "show", unit,
                            "--property=ActiveState,SubState,MainPID,ControlGroup"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=_remaining(remaining, 2), check=False)
    require(shown.returncode == 0, "identity_recovery_worker_unavailable")
    props = dict(line.split("=", 1) for line in shown.stdout.decode().splitlines() if "=" in line)
    require(props.get("ActiveState") == "active" and props.get("SubState") == "running"
            and props.get("MainPID", "").isdigit() and int(props["MainPID"]) > 1,
            "identity_recovery_worker_unavailable")
    pid = int(props["MainPID"])
    process = PROC / str(pid)
    require(process.stat().st_uid == OWNER, "identity_recovery_worker_changed")
    status = (process / "status").read_text()
    require(re.search(r"^Uid:\s+%d\s+%d\s+%d\s+%d\s*$" % ((OWNER,) * 4), status, re.M),
            "identity_recovery_worker_changed")
    argv = (process / "cmdline").read_bytes().split(b"\0")
    require(argv == [b"/usr/bin/python3", str(SCRIPT_HOST).encode(), b"worker", str(number).encode(), b""],
            "identity_recovery_worker_changed")
    fields = (process / "stat").read_text().rpartition(")")[2].split()
    require(len(fields) > 19 and fields[19].isdecimal(), "identity_recovery_worker_changed")
    group = props.get("ControlGroup", "")
    groups = (process / "cgroup").read_text()
    require(group.startswith("/system.slice/") and group.endswith("/" + unit)
            and group in [line.split(":", 2)[2] for line in groups.splitlines() if line.count(":") >= 2],
            "identity_recovery_worker_changed")
    after = (process / "stat").read_text().rpartition(")")[2].split()
    require(len(after) > 19 and after[19] == fields[19], "identity_recovery_worker_changed")
    return {"pid": pid, "start_ticks": int(fields[19]), "unit": unit}


def _recovery_database(folder):
    path = directory(folder) / "runs.db"
    if not path.exists():
        return None
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            "identity_recovery_database_invalid")
    return path, (info.st_dev, info.st_ino)


def _configuration(row):
    value = json.loads(row["config"])
    require(isinstance(value, dict) and value.get("run_id") == row["id"]
            and isinstance(value.get("source"), str), "identity_recovery_config_invalid")
    return value


def _configuration_binding(config, number):
    require(isinstance(config.get("instance_id"), str)
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config["instance_id"])
            and isinstance(config.get("resource_tag"), str) and re.fullmatch(r"[0-9a-f]{12}", config["resource_tag"])
            and isinstance(config.get("expected_container_id"), str)
            and re.fullmatch(r"[0-9a-f]{64}", config["expected_container_id"]), "identity_recovery_binding_invalid")
    return {"device_id": number, "instance_id": config["instance_id"], "resource_tag": config["resource_tag"],
            "container_id": config["expected_container_id"]}


def _recovery_ack(path, request):
    if not os.path.lexists(path):
        return None
    value = read(path)
    keys = {"request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key"}
    require(set(value) == keys | {"decision"} and all(value.get(key) == request.get(key) for key in keys)
            and value.get("decision") in ({"reserved", "collision"} if request["phase"] == "reserve" else {"finalized"}),
            "identity_recovery_ack_changed")
    return value


def _recovery_context(folder, current_run, remaining=None):
    require(isinstance(current_run, str) and re.fullmatch(r"[0-9a-f]{32}", current_run), "identity_run_invalid")
    database = _recovery_database(folder)
    if database is None:
        return None
    path, identity = database
    number = int(Path(folder).name)
    require(number > 0, "identity_recovery_device_invalid")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=_remaining(remaining, .5), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE current=1").fetchall()
        # The supervisor resumes retained work before marking a newly queued
        # run running. Actual MainPID/UID/argv/cgroup proof below is mandatory.
        if len(rows) != 1 or rows[0]["id"] != current_run or rows[0]["state"] not in {"queued", "running", "waiting"}:
            return None
        current_row = dict(rows[0])
        require(type(current_row["iteration"]) is int and current_row["iteration"] >= 0, "identity_recovery_iteration_invalid")
        current_config = _configuration(current_row)
        binding = _configuration_binding(current_config, number)
        short_binding = {key: binding[key] for key in ("instance_id", "resource_tag", "container_id")}
        candidates = []
        for row in connection.execute("SELECT run_id,iteration,operation_key,document FROM regeneration_receipts WHERE run_id != ?", (current_run,)):
            raw = dict(row)
            require(isinstance(raw["document"], str) and len(raw["document"].encode()) <= MAXIMUM,
                    "identity_recovery_receipt_invalid")
            receipt = json.loads(raw["document"])
            require(isinstance(receipt, dict), "identity_recovery_receipt_invalid")
            if receipt.get("binding") == short_binding and receipt.get("status") == "pending":
                candidates.append((raw, receipt))
        if not candidates:
            return None
        require(len(candidates) == 1, "identity_recovery_ambiguous")
        receipt_row, receipt = candidates[0]
        old_run = receipt_row["run_id"]
        require(isinstance(old_run, str) and re.fullmatch(r"[0-9a-f]{32}", old_run), "identity_recovery_config_invalid")
        old = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE id=?", (old_run,)).fetchone()
        require(old is not None, "identity_recovery_original_missing")
        old_row = dict(old)
        require(old_row["current"] == 0 and old_row["state"] in {"stopped", "failed", "completed"}
                and type(receipt_row["iteration"]) is int and receipt_row["iteration"] >= 0
                and receipt_row["iteration"] == old_row["iteration"], "identity_recovery_original_changed")
        original_config = _configuration(old_row)
        require(original_config.get("identity_reservations") is True
                and _configuration_binding(original_config, number) == binding, "identity_recovery_binding_changed")
        root = run_folder(folder, old_run)
        if root is None or not (root / "active.json").exists():
            return None
        active, request = active_request(root, old_run)
        operation = receipt_row["operation_key"]
        require(isinstance(operation, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation),
                "identity_recovery_operation_invalid")
        request_path = root / active["path"]
        owner = read(directory(request_path.parent) / "owner.json")
        require(request_path.parent.name == hashlib.sha256(operation.encode()).hexdigest()
                and request.get("schema") == REQUEST_SCHEMA and request.get("binding") == binding
                and request.get("operation_key") == operation and request.get("phase") in {"reserve", "finalize"}
                and type(request.get("attempt")) is int and 0 <= request["attempt"] < 8
                and request_path.name == "attempt-%d.%s.json" % (request["attempt"], request["phase"])
                and request.get("target_sha256") == digest(request.get("target")), "identity_recovery_request_changed")
        require(all(owner.get(key) == request.get(key) for key in
                    ("binding", "run_id", "operation_key", "native_transaction", "context")), "identity_recovery_owner_changed")
        context = request.get("context")
        require(isinstance(context, dict) and receipt.get("identity_reservation_context") == context
                and context.get("scenario_sha256") == hashlib.sha256(original_config["source"].encode()).hexdigest()
                and context.get("profile_plan_sha256") == (digest(receipt["profile_plan"]) if receipt.get("profile_plan") else None)
                and receipt.get("before_digest") == request.get("native_before", {}).get("stableDigest"),
                "identity_recovery_source_changed")
        location_sidecar(request.get("location_sidecar"), request.get("target"))
        ack = _recovery_ack(request_path.with_suffix(".ack.json"), request)
        if request["phase"] == "reserve":
            require(request.get("values") is None and request.get("native_state") is None
                    and isinstance(request.get("before_values"), dict) and len(request["before_values"]) == 13,
                    "identity_recovery_native_state_changed")
            if ack is not None:
                journal = receipt.get("native_journal")
                if journal is not None:
                    require(ack["decision"] == "reserved" and isinstance(journal, dict)
                            and journal.get("transactionId") == request["native_transaction"]
                            and journal.get("instanceId") == binding["instance_id"]
                            and journal.get("before") == request["native_before"]
                            and journal.get("target") == request["target"], "identity_recovery_native_state_changed")
                return None  # The fixed decision is already delivered; GEN may now be running.
            require(not any(receipt.get(key) is not None for key in
                                ("native_journal", "native_completion", "result", "completed_at")),
                    "identity_recovery_native_state_changed")
        else:
            journal = receipt.get("native_journal")
            require(isinstance(journal, dict) and journal.get("phase") == "committed"
                    and request.get("native_state") == journal and request.get("target") == journal.get("target")
                    and request.get("native_before") == journal.get("before")
                    and journal.get("instanceId") == binding["instance_id"]
                    and request.get("native_transaction") == journal.get("transactionId")
                    and request.get("values") == values_from_target(request["target"], request["location_sidecar"]),
                    "identity_recovery_native_state_changed")
            marker = read(request_path.parent / "prepared.json")
            require(marker == {"native_transaction": request["native_transaction"],
                               "target_sha256": request["target_sha256"], "attempt": request["attempt"]},
                    "identity_recovery_native_state_changed")
            reserved = verified(read(request_path.with_name("attempt-%d.reserve.json" % request["attempt"])), "request_sha256")
            require(reserved.get("phase") == "reserve" and reserved.get("values") is None
                    and reserved.get("native_state") is None and all(reserved.get(key) == request.get(key) for key in
                        ("binding", "run_id", "operation_key", "native_transaction", "context", "target", "native_before", "before_values", "location_sidecar")),
                    "identity_recovery_native_state_changed")
            reserved_ack = read(request_path.with_name("attempt-%d.reserve.ack.json" % request["attempt"]))
            require(reserved_ack.get("decision") == "reserved"
                    and all(reserved_ack.get(key) == reserved.get(key) for key in
                            ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")),
                    "identity_recovery_native_state_changed")
            if ack is not None:
                return None
        connection.execute("COMMIT")
        worker = _recovery_worker(folder, remaining)
        connection.execute("BEGIN")
        latest = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE current=1").fetchall()
        old_again = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE id=?", (old_run,)).fetchone()
        receipt_again = connection.execute("SELECT run_id,iteration,operation_key,document FROM regeneration_receipts WHERE run_id=? AND iteration=? AND operation_key=?",
                                           (old_run, receipt_row["iteration"], operation)).fetchone()
        require(len(latest) == 1 and dict(latest[0]) == current_row and old_again is not None
                and dict(old_again) == old_row and receipt_again is not None and dict(receipt_again) == receipt_row,
                "identity_recovery_state_changed")
        connection.execute("COMMIT")
        after_database = _recovery_database(folder)
        require(after_database is not None and after_database[1] == identity
                and active_request(root, old_run) == (active, request)
                and read(request_path.parent / "owner.json") == owner, "identity_recovery_state_changed")
        envelope = signed({"schema": RECOVERY_SCHEMA, "current_run": current_run,
            "current_iteration": current_row["iteration"], "current_config_sha256": digest(current_config),
            "original_run": old_run, "original_iteration": receipt_row["iteration"],
            "original_config_sha256": digest(original_config), "operation_key": operation,
            "receipt_sha256": digest(receipt), "binding": binding, "worker": worker,
            "request_sha256": request["request_sha256"]}, "proof_sha256")
        return {"envelope": envelope, "request": request, "receipt": receipt,
                "original_config": original_config, "current_config": current_config,
                "binding": binding, "worker": worker}
    finally:
        connection.close()


def recovery_envelope(folder, current_run, remaining=None):
    """Metadata only; the caller must independently obtain a fresh observation."""
    value = _recovery_context(folder, current_run, remaining)
    return value["envelope"] if value is not None else None


def validate_recovery_envelope(folder, current_run, envelope, remaining=None):
    verified(envelope, "proof_sha256")
    require(envelope.get("schema") == RECOVERY_SCHEMA and envelope.get("current_run") == current_run,
            "identity_recovery_envelope_invalid")
    value = _recovery_context(folder, current_run, remaining)
    require(value is not None and value["envelope"] == envelope, "identity_recovery_envelope_changed")
    return {key: value[key] for key in ("request", "receipt", "original_config", "current_config", "binding", "worker")}


def _terminal_worker(folder, remaining=None):
    """Prove the stopped unit has no worker or surviving unit children."""
    number = int(Path(folder).name)
    unit = "xenoid-panel-script@%d.service" % number
    shown = subprocess.run(["systemctl", "show", unit,
                            "--property=ActiveState,SubState,MainPID,ControlPID,ControlGroup"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=_remaining(remaining, 2), check=False)
    require(shown.returncode == 0, "identity_terminal_worker_unavailable")
    props = dict(line.split("=", 1) for line in shown.stdout.decode().splitlines() if "=" in line)
    require((props.get("ActiveState"), props.get("SubState")) in {("inactive", "dead"), ("failed", "failed")}
            and props.get("MainPID") == "0" and props.get("ControlPID") == "0",
            "identity_terminal_worker_active")
    group = props.get("ControlGroup", "")
    if group:
        require(group.startswith("/system.slice/") and group.endswith("/" + unit)
                and ".." not in Path(group).parts, "identity_terminal_worker_changed")
        root = CGROUP_ROOT / group.lstrip("/")
        if root.exists():
            require(not root.is_symlink(), "identity_terminal_worker_changed")
            files = [root / "cgroup.procs", *root.rglob("*/cgroup.procs")]
            require(len(files) <= 4096 and all(not path.read_text().strip() for path in files),
                    "identity_terminal_worker_active")
    return {"unit": unit, "pid": 0, "control_pid": 0,
            "active_state": props["ActiveState"], "sub_state": props["SubState"]}


@contextmanager
def _terminal_native_lock(binding, request):
    """Read-only use of the existing native lock, never creating native state."""
    registry = directory(NATIVE_STATE_HOME)
    instances = registry / "instances"
    info = instances.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not (info.st_mode & 0o022),
            "identity_terminal_native_owner_changed")
    state = directory(instances / binding["instance_id"])
    allocation = read(state / "allocation.json")
    require(allocation.get("instance_id") == binding["instance_id"]
            and allocation.get("resource_tag") == binding["resource_tag"]
            and allocation.get("instance_name") == "phone-%d" % binding["device_id"],
            "identity_terminal_native_owner_changed")
    path = state / "operation.lock"
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == OWNER
            and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1,
            "identity_terminal_native_lock_changed")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                "identity_terminal_native_lock_changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReservationPending("identity_terminal_native_busy") from None
        states = {}
        for name in ("device-regenerate-v3.json", "device-regenerate-v2.json", "device-regenerate.json", "convergence-v1.json"):
            candidate = state / name
            if os.path.lexists(candidate):
                value = read(candidate)
                require(name == "device-regenerate-v3.json"
                        and value.get("instanceId") == binding["instance_id"]
                        and value.get("transactionId") == request["native_transaction"]
                        and value.get("before") == request["native_before"]
                        and value.get("target") == request["target"],
                        "identity_terminal_native_state_changed")
                states[name] = value
        yield
        require(read(state / "allocation.json") == allocation
                and (path.lstat().st_dev, path.lstat().st_ino) == (before.st_dev, before.st_ino),
                "identity_terminal_native_lock_changed")
        for name in ("device-regenerate-v3.json", "device-regenerate-v2.json", "device-regenerate.json", "convergence-v1.json"):
            candidate = state / name
            require(os.path.lexists(candidate) == (name in states)
                    and (name not in states or read(candidate) == states[name]),
                    "identity_terminal_native_state_changed")
    finally:
        os.close(fd)


def terminal_handoff(folder, original_run, remaining=None):
    """Attest preserved terminal work; this never reserves, finalizes or ACKs.

    Only the controller can authorize a same-binding queued successor. Once it
    dispatches that successor, the separate live-worker recovery path applies.
    """
    require(isinstance(original_run, str) and re.fullmatch(r"[0-9a-f]{32}", original_run),
            "identity_run_invalid")
    database = _recovery_database(folder)
    if database is None:
        return None
    path, identity = database
    number = int(Path(folder).name)
    require(number > 0, "identity_recovery_device_invalid")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=_remaining(remaining, .5), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE current=1").fetchall()
        if (len(rows) != 1 or rows[0]["id"] != original_run
                or rows[0]["state"] not in {"stopped", "failed", "completed"}):
            return None
        row = dict(rows[0])
        require(type(row["iteration"]) is int and row["iteration"] >= 0, "identity_recovery_iteration_invalid")
        config = _configuration(row)
        require(config.get("identity_reservations") is True, "identity_terminal_config_changed")
        binding = _configuration_binding(config, number)
        root = run_folder(folder, original_run)
        if root is None or not (root / "active.json").exists():
            return None
        active, request = active_request(root, original_run)
        short_binding = {key: binding[key] for key in ("instance_id", "resource_tag", "container_id")}
        candidates = []
        for item in connection.execute("SELECT run_id,iteration,operation_key,document FROM regeneration_receipts"):
            saved = dict(item)
            require(isinstance(saved["document"], str) and len(saved["document"].encode()) <= MAXIMUM,
                    "identity_recovery_receipt_invalid")
            receipt = json.loads(saved["document"])
            require(isinstance(receipt, dict), "identity_recovery_receipt_invalid")
            if receipt.get("status") == "pending" and receipt.get("binding") == short_binding:
                candidates.append((saved, receipt))
        if not candidates:
            return None
        require(len(candidates) == 1, "identity_recovery_ambiguous")
        saved, receipt = candidates[0]
        operation = saved["operation_key"]
        request_path = root / active["path"]
        owner = read(directory(request_path.parent) / "owner.json")
        require(saved["run_id"] == original_run and saved["iteration"] == row["iteration"]
                and isinstance(operation, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation)
                and request_path.parent.name == hashlib.sha256(operation.encode()).hexdigest()
                and request.get("schema") == REQUEST_SCHEMA and request.get("binding") == binding
                and request.get("operation_key") == operation and request.get("phase") in {"reserve", "finalize"}
                and type(request.get("attempt")) is int and 0 <= request["attempt"] < 8
                and request_path.name == "attempt-%d.%s.json" % (request["attempt"], request["phase"])
                and request.get("target_sha256") == digest(request.get("target")),
                "identity_terminal_request_changed")
        require(all(owner.get(key) == request.get(key) for key in
                    ("binding", "run_id", "operation_key", "native_transaction", "context")),
                "identity_terminal_request_changed")
        context = request.get("context")
        require(isinstance(context, dict) and receipt.get("identity_reservation_context") == context
                and context.get("scenario_sha256") == hashlib.sha256(config["source"].encode()).hexdigest()
                and context.get("profile_plan_sha256") == (digest(receipt["profile_plan"]) if receipt.get("profile_plan") else None)
                and receipt.get("before_digest") == request.get("native_before", {}).get("stableDigest"),
                "identity_recovery_source_changed")
        location_sidecar(request.get("location_sidecar"), request.get("target"))
        journal = receipt.get("native_journal")
        if journal is not None:
            require(isinstance(journal, dict) and journal.get("transactionId") == request["native_transaction"]
                    and journal.get("instanceId") == binding["instance_id"]
                    and journal.get("before") == request["native_before"] and journal.get("target") == request["target"],
                    "identity_terminal_native_state_changed")
        connection.execute("COMMIT")
        with _terminal_native_lock(binding, request):
            worker = _terminal_worker(folder, remaining)
            connection.execute("BEGIN")
            latest = connection.execute("SELECT id,config,state,iteration,current FROM runs WHERE current=1").fetchall()
            again = connection.execute("SELECT run_id,iteration,operation_key,document FROM regeneration_receipts WHERE run_id=? AND iteration=? AND operation_key=?",
                                       (original_run, saved["iteration"], operation)).fetchone()
            require(len(latest) == 1 and dict(latest[0]) == row and again is not None and dict(again) == saved,
                    "identity_terminal_state_changed")
            connection.execute("COMMIT")
            require(_terminal_worker(folder, remaining) == worker
                    and active_request(root, original_run) == (active, request)
                    and read(request_path.parent / "owner.json") == owner
                    and _recovery_database(folder)[1] == identity, "identity_terminal_state_changed")
            proof = signed({"schema": TERMINAL_HANDOFF_SCHEMA, "original_run": original_run,
                "original_iteration": row["iteration"], "original_state": row["state"],
                "original_config_sha256": digest(config), "operation_key": operation,
                "receipt_sha256": digest(receipt), "request_sha256": request["request_sha256"],
                "binding": binding, "worker": worker,
                "native_journal_sha256": digest(journal) if journal is not None else None}, "proof_sha256")
        return proof
    finally:
        connection.close()


def poll(folder, run_id):
    root = run_folder(folder, run_id)
    result = {}
    if root is not None and (root / "observation.json").exists():
        result["identity_observation"] = verified(read(root / "observation.json"), "proof_sha256")
    if root is not None and (root / "active.json").exists():
        result["identity_request"] = active_request(root, run_id)[1]
        handoff = terminal_handoff(folder, run_id)
        if handoff is not None:
            result["identity_terminal_handoff"] = handoff
    else:
        recovery = _recovery_context(folder, run_id)
        if recovery is not None:
            result["identity_request"] = recovery["request"]
            result["identity_recovery"] = recovery["envelope"]
    return result


def acknowledge(folder, run_id, config, packet):
    """Privileged SSH writer; deliberately never takes native EX."""
    if isinstance(packet, dict) and "identity_recovery" in packet:
        value = validate_recovery_envelope(folder, run_id, packet["identity_recovery"])
        require(value["current_config"] == config, "identity_ack_run_changed")
        original_run = packet["identity_recovery"]["original_run"]
        packet = {key: item for key, item in packet.items() if key != "identity_recovery"}
        return acknowledge(folder, original_run, value["original_config"], packet)
    root = run_folder(folder, run_id)
    require(root is not None and config.get("run_id") == run_id, "identity_ack_run_changed")
    active, request = active_request(root, run_id)
    fields = ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")
    require(set(packet) == set(fields) | {"decision"} and all(packet[k] == request[k] for k in fields), "identity_ack_request_changed")
    require(request["binding"]["instance_id"] == config.get("instance_id") and request["binding"]["resource_tag"] == config.get("resource_tag") and request["binding"]["container_id"] == config.get("expected_container_id"), "identity_ack_binding_changed")
    allowed = {"reserved", "collision"} if request["phase"] == "reserve" else {"finalized"}
    require(packet["decision"] in allowed, "identity_ack_decision_invalid")
    path = (root / active["path"]).with_suffix(".ack.json")
    if path.exists():
        require(read(path) == packet, "identity_ack_changed")
    else:
        write_once(path, packet)
    return {"acknowledged": True, "request_sha256": request["request_sha256"]}


class NativeGate:
    def __init__(self, folder, config, manager, operation_key, *, stopping=None, deadline=None, context=None):
        self.folder, self.config, self.manager = Path(folder), config, manager
        self.run = config["run_id"]
        self.root = run_folder(folder, self.run, True)
        require(isinstance(operation_key, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation_key), "identity_operation_invalid")
        self.operation = operation_key
        self.opname = hashlib.sha256(operation_key.encode()).hexdigest()
        self.path = directory(self.root / self.opname, True)
        expected_container = config.get("expected_container_id")
        require(isinstance(expected_container, str) and re.fullmatch(r"[0-9a-f]{64}", expected_container), "identity_expected_container_required")
        self.binding = {"device_id": int(self.folder.name), "instance_id": manager.context.instance_id,
            "resource_tag": manager.context.resource_tag, "container_id": expected_container}
        self.stopping, self.deadline, self.context = stopping, deadline, context or {}

    def transaction_id(self, create=True):
        path = self.path / "owner.json"
        if not path.exists():
            require(create, "prepared_identity_owner_missing")
            write_once(path, {"binding": self.binding, "run_id": self.run, "operation_key": self.operation,
                              "native_transaction": secrets.token_hex(16), "context": self.context})
        value = read(path)
        require(value["binding"] == self.binding and value["run_id"] == self.run and value["operation_key"] == self.operation and value["context"] == self.context, "identity_owner_changed")
        return value["native_transaction"]

    def _request(self, attempt, phase, before, before_values, target, location, *, values=None, state=None):
        path = self.path / ("attempt-%d.%s.json" % (attempt, phase))
        value = signed({"schema": REQUEST_SCHEMA, "binding": self.binding, "run_id": self.run,
            "operation_key": self.operation, "attempt": attempt, "native_transaction": self.transaction_id(),
            "phase": phase, "native_before": before, "before_values": before_values, "target": target,
            "target_sha256": digest(target), "location_sidecar": location_sidecar(location, target),
            "context": self.context, "values": values, "native_state": state}, "request_sha256")
        if path.exists():
            require(read(path) == value, "identity_request_changed")
        else:
            write_once(path, value)
        replace_owned(self.root / "active.json", {"path": str(path.relative_to(self.root)), "sha256": value["request_sha256"]})
        return value, path

    def _wait(self, value, path):
        while True:
            ack = path.with_suffix(".ack.json")
            if ack.exists():
                answer = read(ack)
                for key in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key"):
                    require(answer.get(key) == value[key], "identity_ack_changed")
                require(answer.get("decision") in ({"reserved", "collision"} if value["phase"] == "reserve" else {"finalized"}), "identity_ack_invalid")
                return answer["decision"]
            if self.stopping is not None and self.stopping.is_set():
                raise ReservationPending("identity_reservation_stopping")
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ReservationPending("identity_reservation_pending")
            time.sleep(.1)

    def reserve(self, before, factory):
        current, _ = observe(self.manager)
        require(digest({"androidId": current["android_id"], "serial": current["serial"], "imei": current["imei"], "imeisv": current["imeisv"]}) == before["stableDigest"], "identity_before_changed")
        for external, native in (("boot_id", "bootId"), ("bluetooth_address", "bluetoothAddress"),
                                 ("hostname", "hostname"), ("device_name", "deviceName"),
                                 ("statfs_fsid", "statfsFsid"), ("drm_device_unique_id", "drmDeviceUniqueId")):
            if native in before:
                expected = before[native].upper() if external == "bluetooth_address" else before[native]
                require(current[external] == expected, "identity_before_changed")
        for attempt in range(8):
            path = self.path / ("attempt-%d.reserve.json" % attempt)
            if path.exists():
                old = verified(read(path), "request_sha256")
                require(old["native_before"] == before and old["before_values"] == current, "identity_before_changed")
                target = old["target"]
                location = old["location_sidecar"]
            else:
                require(not (self.path / "prepared.json").exists(), "prepared_identity_cannot_reroll")
                self._candidate_location = None
                target = factory()
                location = self._candidate_location
            request, path = self._request(attempt, "reserve", before, current, target, location)
            if self._wait(request, path) == "reserved":
                return target
            require(not (self.path / "prepared.json").exists(), "prepared_identity_cannot_reroll")
        raise ReservationPending("identity_collision_attempt_limit")

    def location_target(self, value):
        self._candidate_location = json.loads(canonical(location_sidecar(value)))

    def _matching(self, state):
        require(state.get("schema") == "dev.xenoid.device-regenerate/v3" and state.get("before", {}).get("containerId") == self.binding["container_id"], "identity_native_scope_changed")
        require(state["transactionId"] == self.transaction_id(create=False) and state["instanceId"] == self.binding["instance_id"], "identity_native_transaction_changed")
        for attempt in range(8):
            path = self.path / ("attempt-%d.reserve.json" % attempt)
            if path.exists():
                value = verified(read(path), "request_sha256")
                if value["target_sha256"] == digest(state["target"]) and value["native_before"] == state["before"]:
                    location_sidecar(value["location_sidecar"], state["target"])
                    require(self._wait(value, path) == "reserved", "prepared_identity_not_reserved")
                    return attempt, value
        raise ReservationPending("prepared_identity_not_reserved")

    def prepared(self, state):
        attempt, value = self._matching(state)
        path = self.path / "prepared.json"
        marker = {"native_transaction": state["transactionId"], "target_sha256": value["target_sha256"], "attempt": attempt}
        if path.exists():
            require(read(path) == marker, "prepared_identity_changed")
        else:
            write_once(path, marker)

    resume = prepared

    def finalize(self, state):
        require(state.get("phase") == "committed", "identity_native_commit_required")
        attempt, old = self._matching(state)
        location = old["location_sidecar"]
        values, _ = observe(self.manager, expected_location=location)
        require(values == values_from_target(state["target"], location), "identity_committed_readback_changed")
        request, path = self._request(attempt, "finalize", state["before"], old["before_values"], state["target"], location, values=values, state=state)
        require(self._wait(request, path) == "finalized", "identity_finalization_pending")
        publish_observation(self.folder, self.config, self.manager)
