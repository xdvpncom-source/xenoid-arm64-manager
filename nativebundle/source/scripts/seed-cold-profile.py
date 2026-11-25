#!/usr/bin/env python3
"""First-profile publication owned by the existing pre-create boot seed.

Input arrives only on stdin. Existing userdata and conflicting partial seeds
are rejected. DeviceIdentityStore owns every supplied ID; this helper generates
none and never changes a preexisting identity leaf.
"""
from __future__ import annotations
import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

MAX_INPUT = 512 * 1024
LABELS = {"/local": (0, 0, 0o751, "u:object_r:system_data_file:s0"),
          "/local/tmp": (2000, 2000, 0o771, "u:object_r:shell_data_file:s0"),
          "/local/tmp/xenoid-profile": (0, 0, 0o700, "u:object_r:shell_data_file:s0")}
FILE_LABEL = "u:object_r:shell_data_file:s0"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

class SeedError(Exception):
    pass

def require(condition, code):
    if not condition:
        raise SeedError(code)

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def unique_object(pairs):
    output = {}
    for key, value in pairs:
        require(key not in output, "duplicate_json_key")
        output[key] = value
    return output

def checked(command, *, accepted=(0,), timeout=40):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode in accepted, "seed_tool_failed")
    return result.stdout

def scalar(value):
    require(type(value) in (str, int), "profile_leaf_invalid")
    text = str(value)
    require(text and len(text) < 512 and all(32 <= ord(c) < 127 for c in text), "profile_leaf_invalid")
    return (text + "\n").encode()

def initial_bluetooth_address(ids):
    # Stable native identity owns the initial address; no random generator or
    # runtime API is involved. Locally administered, unicast, 46-bit identity.
    raw = bytearray(hashlib.sha256(b"xenoid.bluetooth-initial/v1\0" +
        ids["serial"].encode("ascii") + b"\0" + ids["android_id"].encode("ascii")).digest()[:6])
    raw[0] = (raw[0] | 2) & 0xfe
    return ":".join("%02X" % value for value in raw)


def valid_bluetooth_sidecar(value):
    return isinstance(value, bytes) and re.fullmatch(rb"[0-9A-F]{2}(?::[0-9A-F]{2}){5}\n", value) is not None


def initial_runtime_identity(stable, model):
    """Deterministic initial sidecars from already-owned stable identity."""
    serial = stable["serial"]
    android_id = stable.get("androidId", stable.get("android_id"))
    if (not isinstance(serial, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,31}", serial) is None
            or not isinstance(android_id, str) or re.fullmatch(r"[0-9a-f]{16}", android_id) is None
            or not isinstance(model, str) or re.fullmatch(r"[ -~]{1,50}", model) is None):
        raise ValueError("initial_identity_input_invalid")
    seed = b"xenoid.initial-runtime/v1\0" + serial.encode("ascii") + b"\0" + android_id.encode("ascii") + b"\0"
    def derive(name):
        return hashlib.sha256(seed + name).hexdigest()
    fsid = derive(b"statfs")[:16]
    return {"statfs_fsid": fsid if int(fsid, 16) else "0000000000000001",
            "hostname": "android-" + derive(b"hostname")[:16],
            "device_name": model + " " + derive(b"device-name")[:12].upper()}


def profile_leaves(profile):
    require(isinstance(profile, dict) and profile.get("schema") == "dev.xenoid.fingerprint/v1", "profile_schema_invalid")
    require(profile.get("build", {}).get("device") == "bluejay", "profile_product_invalid")
    ids = profile.get("ids", {})
    require(set(ids) == {"android_id", "boot_id", "serial", "imei", "imeisv"}, "identity_fields_invalid")
    require(all(isinstance(value, str) and value != "REGENERATE" for value in ids.values()), "identity_invalid")
    require(re.fullmatch(r"[0-9a-f]{16}", ids["android_id"] or "") is not None, "identity_invalid")
    require(UUID.fullmatch(ids["boot_id"] or "") is not None, "identity_invalid")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,31}", ids["serial"] or "") is not None, "identity_invalid")
    require(re.fullmatch(r"[0-9]{15}", ids["imei"] or "") is not None, "identity_invalid")
    require(re.fullmatch(r"[0-9]{2}", ids["imeisv"] or "") is not None, "identity_invalid")
    output = {key: scalar(value) for key, value in ids.items()}
    output["bluetooth_address"] = scalar(initial_bluetooth_address(ids))
    output.update({key: scalar(value) for key, value in initial_runtime_identity(ids, profile["build"]["model"]).items()})
    cpu = profile["cpu"]
    require(len(cpu["cores"]) == 8, "cpu_profile_invalid")
    output.update(cpu_count=b"8\n", cpu_implementer=scalar(cpu["implementer"]))
    for index, core in enumerate(cpu["cores"]):
        for key in ("processor", "part", "model", "minimumFrequencyKhz", "maximumFrequencyKhz"):
            output[f"cpu_{index}_{key}"] = scalar(core[key])
    for key in ("totalBytes", "totalKiB", "swapBytes"):
        output["memory_" + key] = scalar(profile["memory"][key])
    for key in ("capacityMah", "minimumCapacityMah", "chargeFullDesignUah", "chargeFullUah", "chargeCounterUah", "health", "level", "plugged", "present", "scale", "status", "technology", "temperature", "voltage"):
        output["battery_" + key] = scalar(profile["battery"][key])
    for key in ("width", "height", "densityDpi", "physicalPpi", "defaultRefreshRateHz", "peakRefreshRateHz"):
        output["display_" + ("density_dpi" if key == "densityDpi" else key)] = scalar(profile["display"][key])
    for key in ("name", "busType", "vendorId", "productId", "version"):
        output["input_" + key] = scalar(profile["input"][key])
    output["input_width"] = scalar(profile["display"]["width"])
    output["input_height"] = scalar(profile["display"]["height"])
    for axis in ("x", "y", "pressure", "trackingId"):
        for key in ("minimum", "maximum"):
            output[f"input_{axis}_{key}"] = scalar(profile["input"][axis][key])
    for key in ("manufacturer", "product", "vendor_id", "product_id"):
        output["usb_" + key] = scalar(profile["usb"][key])
    for index in range(5):
        for key in ("type", "temp"):
            output[f"thermal_zone{index}_{key}"] = scalar(profile["thermal"][f"zone{index}"][key])
    for key in ("capacityBytes", "blockDevice", "mountSource", "sectorSizeBytes", "sectorCount", "technology", "filesystem"):
        output["storage_" + key] = scalar(profile["storage"][key])
    output["effective.json"] = canonical(profile) + b"\n"
    return output

def validate_policy(policy, leaves):
    # Review only rules that actually cover one of the paths this helper
    # publishes. Other /data/local/tmp trees can intentionally use a different
    # label (for example Android's /data/local/tmp/ltp test directory).
    paths = {"/data/local/tmp", "/data/local/tmp/xenoid-profile"}
    for name in leaves:
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name), "profile_leaf_invalid")
        paths.add("/data/local/tmp/xenoid-profile/" + name)
    lines = []
    for line in policy.decode().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            parts = line.split()
            if len(parts) >= 2:
                lines.append((parts[0], parts[-1]))
    require((r"/data/local/tmp(/.*)?", FILE_LABEL) in lines, "image_profile_label_policy_missing")
    # Reject every conflicting matching override. This deliberately requires
    # agreement rather than guessing SELinux regex precedence. Python's
    # supported regex subset is sufficient for these selected-image rules;
    # unsupported relevant expressions fail closed before any image write.
    for pattern, label in lines:
        if pattern.startswith("/data/local/tmp"):
            try:
                matched = any(re.fullmatch(pattern, path, flags=re.DOTALL) is not None for path in paths)
            except re.error as error:
                raise SeedError("image_profile_label_policy_unsupported") from error
            if matched:
                require(label == FILE_LABEL, "image_profile_label_policy_conflict")
    return True

class Image:
    def __init__(self, path):
        self.path = str(path)
    def command(self, command):
        require("\n" not in command and "\r" not in command, "debugfs_command_invalid")
        return checked(["debugfs", "-w", "-R", command, self.path])
    def read(self, path):
        return checked(["debugfs", "-R", "cat " + path, self.path])
    def info(self, path):
        return checked(["debugfs", "-R", "stat " + path, self.path]).decode()
    def exists(self, path):
        return "Inode:" in self.info(path)
    def names(self, path):
        raw = checked(["debugfs", "-R", "ls -p " + path, self.path]).decode()
        return {line.split("/")[5] for line in raw.splitlines() if line.startswith("/") and len(line.split("/")) > 6} - {".", ".."}
    def ownership_matches(self, path, uid, gid, mode):
        info = self.info(path)
        owner = re.search(r"User:\s*" + str(uid) + r"\s+Group:\s*" + str(gid) + r"\b", info)
        match = re.search(r"Mode:\s*0*([0-7]+)", info)
        return owner is not None and match is not None and int(match[1], 8) == mode
    def label(self, path):
        with tempfile.TemporaryDirectory(prefix="xenoid-context-") as temp:
            target = Path(temp) / "readback"
            checked(["debugfs", "-R", f"ea_get -f {target} {path} security.selinux", self.path])
            return target.read_bytes() if target.exists() else None
    def metadata_matches(self, path, uid, gid, mode, context):
        return self.ownership_matches(path, uid, gid, mode) and self.label(path) == context.encode() + b"\0"
    def set_label(self, path, context):
        with tempfile.TemporaryDirectory(prefix="xenoid-context-") as temp:
            source = Path(temp) / "context"
            source.write_bytes(context.encode() + b"\0")
            source.chmod(0o600)
            self.command(f"ea_set -f {source} {path} security.selinux")
        require(self.label(path) == context.encode() + b"\0", "profile_metadata_readback_failed")
    def metadata(self, path, uid, gid, mode, context):
        kind = stat.S_IFDIR if "Type: directory" in self.info(path) else stat.S_IFREG
        self.command(f"sif {path} uid {uid}")
        self.command(f"sif {path} gid {gid}")
        self.command(f"sif {path} mode 0{kind | mode:o}")
        self.set_label(path, context)
        require(self.metadata_matches(path, uid, gid, mode, context), "profile_metadata_readback_failed")
    def write_absent(self, path, data):
        if self.exists(path):
            require(self.read(path) == data, "profile_seed_conflict")
            return
        with tempfile.TemporaryDirectory(prefix="xenoid-profile-") as temp:
            source = Path(temp) / "leaf"
            source.write_bytes(data)
            source.chmod(0o600)
            self.command(f"write {source} {path}")
        require(self.read(path) == data, "profile_seed_readback_failed")

def classify_seed(image, profile, *, policy):
    leaves = profile_leaves(profile)
    validate_policy(policy, leaves)
    fresh = image.names("/") <= {"lost+found", "local"}
    if image.exists("/local"):
        fresh = fresh and image.names("/local") <= {"tmp"}
    if image.exists("/local/tmp"):
        fresh = fresh and image.names("/local/tmp") <= {"xenoid-profile"}
    directory = "/local/tmp/xenoid-profile"
    if not fresh:
        # initialize_replace boots once, then creates its final container before
        # host identity.active is committed. Recognize only our complete fixed
        # profile here; this branch performs no filesystem repair or writes.
        require(image.exists(directory + "/effective.json"), "userdata_not_fresh")
        stored = json.loads(image.read(directory + "/effective.json"), object_pairs_hook=unique_object)
        expected = deepcopy(profile)
        boot = stored.get("ids", {}).get("boot_id")
        require(isinstance(boot, str) and UUID.fullmatch(boot) is not None, "profile_seed_conflict")
        expected["ids"]["boot_id"] = boot
        require(stored == expected, "profile_seed_conflict")
        expected_leaves = profile_leaves(expected)
        for path, metadata in LABELS.items():
            require("Type: directory" in image.info(path), "profile_path_not_directory")
            modes = (0o700, 0o755) if path == directory else (metadata[2],)
            require(any(image.metadata_matches(path, metadata[0], metadata[1], mode, metadata[3])
                        for mode in modes), "profile_metadata_mismatch")
        repairs = []
        for name, data in expected_leaves.items():
            path = directory + "/" + name
            require(image.exists(path) and "Type: regular" in image.info(path), "profile_seed_incomplete")
            actual = image.read(path)
            if name == "boot_id":
                require(UUID.fullmatch(actual.decode().strip()) is not None, "profile_seed_conflict")
            elif name == "effective.json":
                require(json.loads(actual) == expected, "profile_seed_conflict")
            elif name in {"statfs_fsid", "hostname", "device_name"}:
                text = actual.decode("ascii").strip()
                valid = (re.fullmatch(r"[0-9a-f]{16}", text) is not None and int(text, 16) != 0 if name == "statfs_fsid"
                         else re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", text) is not None if name == "hostname"
                         else re.fullmatch(r"[ -~]{1,64}", text) is not None)
                require(valid and actual == (text + "\n").encode(), "profile_seed_conflict")
            elif name == "bluetooth_address":
                # Completed/pending GEN may own a newer address than the
                # initial derivation. Validate it without ever replacing it.
                require(valid_bluetooth_sidecar(actual), "profile_seed_conflict")
            else:
                require(actual == data, "profile_seed_conflict")
            mode = 0o600 if name in {"bluetooth_address", "hostname", "device_name"} else 0o644
            if not image.metadata_matches(path, 0, 0, mode, FILE_LABEL):
                # Restore only an absent label on known atomic-writer leaves.
                # All profile/leaf values are validated before seed_image writes
                # any labels; current/GEN-owned sidecar values stay unchanged.
                repairable = name.startswith(("cpu_", "memory_", "battery_", "display_", "input_", "thermal_")) or name in {"usb_manufacturer", "usb_product", "usb_vendor_id", "usb_product_id", "statfs_fsid", "hostname"}
                require(repairable and image.ownership_matches(path, 0, 0, mode)
                        and re.search(r"Links:\s*1\b", image.info(path)) is not None
                        and image.label(path) in (None, b""), "profile_metadata_mismatch")
                repairs.append(path)
        return "initialized", tuple(repairs)
    if image.exists(directory):
        require(image.names(directory) <= set(leaves), "profile_seed_conflict")
    for name, data in leaves.items():
        path = directory + "/" + name
        if image.exists(path):
            require("Type: regular" in image.info(path) and image.read(path) == data, "profile_seed_conflict")
    return "fresh", ()


def seed_image(image, profile, *, policy):
    classification, repairs = classify_seed(image, profile, policy=policy)
    if classification == "initialized":
        for path in repairs:
            image.set_label(path, FILE_LABEL)
        require(classify_seed(image, profile, policy=policy) == ("initialized", ()), "profile_metadata_readback_failed")
        return {"ok": True, "alreadyInitialized": True, "identityGenerated": False, "labelsRepaired": len(repairs)}
    leaves = profile_leaves(profile)
    for path, (uid, gid, mode, context) in LABELS.items():
        if not image.exists(path):
            image.command("mkdir " + path)
        require("Type: directory" in image.info(path), "profile_path_not_directory")
        image.metadata(path, uid, gid, mode, context)
    for name in [key for key in leaves if key != "effective.json"] + ["effective.json"]:
        path = "/local/tmp/xenoid-profile/" + name
        image.write_absent(path, leaves[name])
        image.metadata(path, 0, 0, 0o600 if name in {"bluetooth_address", "hostname", "device_name"} else 0o644, FILE_LABEL)
    return {"ok": True, "profileSeeded": True, "leafCount": len(leaves), "identityGenerated": False}


def safe_regular(path):
    path = Path(path)
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), "unsafe_image_path")
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not info.st_mode & 0o022, "unsafe_image_file")
    return path

def validate_request(request):
    require(isinstance(request, dict) and set(request) == {"volume", "labels", "dataUuid", "rootfsUuid", "profile"}, "seed_request_invalid")
    labels = request["labels"]
    require(isinstance(labels, dict) and set(labels) == {"dev.xenoid.owner", "dev.xenoid.schema", "dev.xenoid.instance_id", "dev.xenoid.instance_name", "dev.xenoid.resource_tag"}, "volume_ownership_mismatch")
    require(labels["dev.xenoid.owner"] == "xenoid" and labels["dev.xenoid.schema"] == "1", "volume_ownership_mismatch")
    instance = labels["dev.xenoid.instance_id"]
    require(isinstance(instance, str) and UUID.fullmatch(instance), "volume_ownership_mismatch")
    tag = hashlib.sha256(b"xenoid-instance/v1\0" + instance.encode("ascii")).hexdigest()[:12]
    require(labels["dev.xenoid.resource_tag"] == tag and request["volume"] == "xenoid-data-" + tag, "volume_ownership_mismatch")
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", labels["dev.xenoid.instance_name"] or "") is not None, "volume_ownership_mismatch")
    require(UUID.fullmatch(request["dataUuid"] or "") and UUID.fullmatch(request["rootfsUuid"] or ""), "storage_uuid_invalid")

def main():
    require(os.geteuid() == 0, "root_required")
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    require(0 < len(raw) <= MAX_INPUT, "seed_input_invalid")
    request = json.loads(raw, object_pairs_hook=unique_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(SeedError("nonfinite_json")))
    validate_request(request)
    volume = request["volume"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", volume or "") is not None, "volume_invalid")
    require(UUID.fullmatch(request["dataUuid"] or "") and UUID.fullmatch(request["rootfsUuid"] or ""), "storage_uuid_invalid")
    records = json.loads(checked(["docker", "volume", "inspect", volume]))
    require(len(records) == 1 and all(records[0].get("Labels", {}).get(k) == v for k, v in request["labels"].items()), "volume_ownership_mismatch")
    users = checked(["docker", "ps", "-q", "--filter", "volume=" + volume]).strip()
    require(not users, "volume_in_use")
    base = Path(records[0]["Mountpoint"])
    data = safe_regular(base / "xenoid-data.img")
    rootfs = safe_regular(base / "xenoid-rootfs.img")
    for path, expected in ((data, request["dataUuid"]), (rootfs, request["rootfsUuid"])):
        actual = checked(["blkid", "-p", "-s", "UUID", "-o", "value", str(path)]).decode().strip().lower()
        require(actual == expected, "storage_uuid_mismatch")
        require(not checked(["losetup", "-j", str(path)]).strip(), "image_still_attached")
    root_image = Image(rootfs)
    policy = root_image.read("/system/etc/selinux/plat_file_contexts")
    for context_path in (
        "/vendor/etc/selinux/vendor_file_contexts",
        "/system/system_ext/etc/selinux/system_ext_file_contexts",
        "/system/product/etc/selinux/product_file_contexts",
        "/odm/etc/selinux/odm_file_contexts",
    ):
        if root_image.exists(context_path):
            policy += b"\n" + root_image.read(context_path)
    image = Image(data)
    classification, repairs = classify_seed(image, request["profile"], policy=policy)
    if classification == "initialized" and not repairs:
        print('{"ok":true,"alreadyInitialized":true,"identityGenerated":false}')
        return
    checked(["e2fsck", "-pf", str(data)], accepted=(0, 1), timeout=600)
    require(not checked(["docker", "ps", "-q", "--filter", "volume=" + volume]).strip(), "volume_in_use")
    require(not checked(["losetup", "-j", str(data)]).strip(), "image_still_attached")
    result = seed_image(image, request["profile"], policy=policy)
    checked(["e2fsck", "-pf", str(data)], accepted=(0, 1), timeout=600)
    fd = os.open(data, os.O_RDONLY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    print(json.dumps(result, separators=(",", ":")))

if __name__ == "__main__":
    try:
        main()
    except (SeedError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as failure:
        # Only our fixed error codes are public; never echo request/tool data.
        code = str(failure) if isinstance(failure, SeedError) else "cold_profile_seed_failed"
        print(json.dumps({"ok": False, "error": code}, separators=(",", ":")))
        sys.exit(1)
