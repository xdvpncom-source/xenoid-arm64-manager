#!/usr/bin/env python3
"""Own the canonical Android TUN node without touching Linux's /dev/net/tun.

Executed on the Docker engine host by RuntimeManager.  Guest stat/SELinux
wrappers are deliberately not used: all metadata below comes from host syscalls
through the exact, twice-inspected container init process.  No ioctl is issued
and no network interface, rule, application, or persistent data is changed.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
import sys
import uuid

SCHEMA = "dev.xenoid.android-tun/v1"
LABEL = b"u:object_r:tun_device:s0\0"
DEVICE = os.makedev(10, 200)


def require(value, code):
    if not value:
        raise RuntimeError(code)


def validate_node(meta, label):
    require(stat.S_ISCHR(meta.st_mode) and meta.st_rdev == DEVICE,
            "android_tun_node_conflict")
    require((meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) == (1000, 1016, 0o660),
            "android_tun_metadata_conflict")
    require(label.rstrip(b"\0") == LABEL.rstrip(b"\0") and b"\0" not in label.rstrip(b"\0"),
            "android_tun_label_conflict")


def node(fd, name):
    try:
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    # /proc/self/fd resolves the already-open directory, never a caller path.
    try:
        label = os.getxattr(f"/proc/self/fd/{fd}/{name}", "security.selinux", follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
        label = b""
    after = os.stat(name, dir_fd=fd, follow_symlinks=False)
    require(same_inode(value, after), "android_tun_node_changed")
    return after, label


def same_inode(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def ensure_node(fd, *, repair, checkpoint):
    """A missing node can be published; an unknown existing inode is never edited."""
    existing = node(fd, "tun")
    if existing is not None:
        validate_node(*existing)
        checkpoint()
        return {"ok": True, "ready": True, "created": False}
    if not repair:
        checkpoint()
        return {"ok": False, "ready": False, "created": False,
                "error": "android_tun_missing"}
    checkpoint()
    temporary = ".xenoid-tun-" + uuid.uuid4().hex
    created = published = None
    success = False
    try:
        os.mknod(temporary, stat.S_IFCHR | 0o600, DEVICE, dir_fd=fd)
        created = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
        os.chown(temporary, 1000, 1016, dir_fd=fd, follow_symlinks=False)
        os.chmod(temporary, 0o660, dir_fd=fd, follow_symlinks=False)
        os.setxattr(f"/proc/self/fd/{fd}/{temporary}", "security.selinux", LABEL,
                    follow_symlinks=False)
        validate_node(*node(fd, temporary))
        checkpoint()
        # link() is exclusive: an unexpected concurrent node is not replaced.
        os.link(temporary, "tun", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        published = os.stat("tun", dir_fd=fd, follow_symlinks=False)
        require(same_inode(created, published), "android_tun_publication_changed")
        os.unlink(temporary, dir_fd=fd)
        validate_node(*node(fd, "tun"))
        checkpoint()
        success = True
        return {"ok": True, "ready": True, "created": True}
    finally:
        # Roll back only our new inode, never an unknown replacement.
        for name, expected in ((temporary, created), ("tun", published if not success else None)):
            if expected is not None:
                try:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if same_inode(current, expected):
                        os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass


def inspect_container(expected):
    proc = subprocess.run(["docker", "inspect", expected["containerId"]],
                          capture_output=True, text=True, timeout=10)
    require(proc.returncode == 0, "android_tun_container_unavailable")
    result = json.loads(proc.stdout)
    require(isinstance(result, list) and len(result) == 1, "android_tun_container_ambiguous")
    item = result[0]
    labels = item.get("Config", {}).get("Labels", {}) or {}
    state = item.get("State", {})
    require(item.get("Id") == expected["containerId"]
            and item.get("Image") == expected["imageId"]
            and labels.get("dev.xenoid.instance_id") == expected["instanceId"]
            and labels.get("dev.xenoid.resource_tag") == expected["resourceTag"]
            and state.get("Running") is True
            and type(state.get("Pid")) is int and state["Pid"] > 1
            and isinstance(state.get("StartedAt"), str) and state["StartedAt"]
            and item.get("HostConfig", {}).get("Privileged") is True,
            "android_tun_container_conflict")
    pid = state["Pid"]
    with open(f"/proc/{pid}/stat") as stream:
        parts = stream.read().rsplit(")", 1)[1].split()
    require(len(parts) > 19 and parts[19].isdigit(), "android_tun_pid_invalid")
    return pid, state["StartedAt"], parts[19]


def run(expected, *, repair):
    require(os.geteuid() == 0, "android_tun_engine_root_required")
    require(expected.get("schema") == SCHEMA, "android_tun_request_invalid")
    for key, pattern in (("containerId", r"[0-9a-f]{64}"),
                         ("imageId", r"sha256:[0-9a-f]{64}"),
                         ("instanceId", r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"),
                         ("resourceTag", r"[0-9a-f]{12}")):
        require(isinstance(expected.get(key), str) and re.fullmatch(pattern, expected[key]),
                "android_tun_request_invalid")
    owner = inspect_container(expected)
    def checkpoint():
        require(inspect_container(expected) == owner, "android_tun_runtime_changed")
    root = os.open(f"/proc/{owner[0]}/root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Pin the metadata to this image's actual Android policy, not a new
        # rule or an assumption about the host's SELinux enforcement mode.
        def policy(path):
            with open(f"/proc/self/fd/{root}/{path}") as stream:
                text = stream.read(2 * 1024 * 1024 + 1)
            require(len(text) <= 2 * 1024 * 1024, "android_tun_policy_invalid")
            return [line.split("#", 1)[0].split() for line in text.splitlines()]
        uevent = [line for line in policy("system/etc/ueventd.rc") if line and line[0] == "/dev/tun"]
        contexts = [line for line in policy("system/etc/selinux/plat_file_contexts") if line and line[0] == "/dev/tun"]
        require(uevent == [["/dev/tun", "0660", "system", "vpn"]]
                and contexts == [["/dev/tun", LABEL.rstrip(b"\0").decode()]],
                "android_tun_policy_conflict")
        fd = os.open("dev", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            directory = os.fstat(fd)
            require(directory.st_uid == 0 and not stat.S_IMODE(directory.st_mode) & 0o022,
                    "android_tun_directory_conflict")
            result = ensure_node(fd, repair=repair, checkpoint=checkpoint)
        finally:
            os.close(fd)
    finally:
        os.close(root)
    return {"schema": SCHEMA, "containerId": expected["containerId"], **result}


def main():
    try:
        require(len(sys.argv) == 3 and sys.argv[1] in {"inspect", "ensure"},
                "android_tun_request_invalid")
        result = run(json.loads(sys.argv[2]), repair=sys.argv[1] == "ensure")
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) else "android_tun_operation_failed"
        result = {"schema": SCHEMA, "ok": False, "ready": False, "error": code}
        if isinstance(exc, OSError):
            result["errno"] = exc.errno
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
