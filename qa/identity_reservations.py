"""Managed GEN reservation over existing privileged SSH sync; no network service."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

OWNER = 0
PROC = Path("/proc")
RADIO_UID = 1001
RADIO_GID = 1001
MAXIMUM = 512 * 1024
REQUEST_SCHEMA = "dev.xenoid.native-identity-request/v1"
OBSERVATION_SCHEMA = "dev.xenoid.native-identity-observation/v1"


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


def poll(folder, run_id):
    root = run_folder(folder, run_id)
    if root is None:
        return {}
    result = {}
    if (root / "observation.json").exists():
        result["identity_observation"] = verified(read(root / "observation.json"), "proof_sha256")
    if (root / "active.json").exists():
        result["identity_request"] = active_request(root, run_id)[1]
    return result


def acknowledge(folder, run_id, config, packet):
    """Privileged SSH writer; deliberately never takes native EX."""
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
