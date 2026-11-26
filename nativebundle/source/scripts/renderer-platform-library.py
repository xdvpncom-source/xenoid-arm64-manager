#!/usr/bin/env python3
"""Resolve one exact Android GLES producer without changing existing build inputs.

Uses the platform-library cache pattern: immutable base image, stopped disposable
container, pinned bytes, atomic private-cache publication. It never starts a
container, reads an instance, modifies a runtime, or downloads an alternate ELF.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

BASE = "sha256:5a42a569ee1d7c71796c0385e906cbaa4c3e0a162a56d9f26b29bdb1befac13b"
SHA = "8385182775899793a5831cd9535b07b8d993b8582e4c4bbbf58d06e828f5216d"
SIZE = 8030848
NAME = "libGLESv2_angle.so"
TARGET = "/vendor/lib64/egl/" + NAME


def require(value, code):
    if not value:
        raise RuntimeError("renderer_platform_" + code)


def valid(path):
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_nlink == 1
                and info.st_uid == os.getuid() and not info.st_mode & 0o022
                and info.st_size == SIZE and hashlib.sha256(path.read_bytes()).hexdigest() == SHA)
    except OSError:
        return False


def run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    require(result.returncode == 0, "docker_operation_failed")
    return result.stdout.decode("utf-8", "strict").strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cache = args.cache or root / ".xenoid/cache/platform-renderer"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = cache.lstat()
    require(stat.S_ISDIR(info.st_mode) and not cache.is_symlink()
            and info.st_uid == os.getuid() and not info.st_mode & 0o077, "cache_unsafe")
    parent = cache / SHA
    parent.mkdir(exist_ok=True, mode=0o700)
    info = parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink()
            and info.st_uid == os.getuid() and not info.st_mode & 0o077, "cache_unsafe")
    output = parent / NAME
    if valid(output):
        print(output.resolve())
        return
    contexts = []
    for value in (os.environ.get("XENOID_DOCKER_CONTEXT"), os.environ.get("DOCKER_CONTEXT"),
                  "colima" if os.uname().sysname == "Darwin" else None, "default"):
        if value and value not in contexts:
            contexts.append(value)
    docker = None
    for context in contexts:
        candidate = ["docker", "--context", context]
        probe = subprocess.run(candidate + ["image", "inspect", "--format", "{{.Id}}", BASE],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if probe.returncode == 0 and probe.stdout.decode().strip() == BASE:
            docker = candidate
            break
    require(docker is not None, "pinned_base_missing")
    container = None
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=cache) as temporary:
        try:
            container = run(docker + ["create", BASE])
            require(re.fullmatch(r"[0-9a-f]{64}", container), "container_identity_invalid")
            source = Path(temporary) / NAME
            run(docker + ["cp", "-L", container + ":" + TARGET, str(source)])
            require(valid(source), "base_bytes_changed")
            source.chmod(0o644)
            with source.open("rb") as stream:
                os.fsync(stream.fileno())
            require(not output.is_symlink(), "cache_symlink")
            os.replace(source, output)
        finally:
            if container and re.fullmatch(r"[0-9a-f]{64}", container):
                run(docker + ["rm", "-f", container])
    require(valid(output), "cache_verification_failed")
    print(output.resolve())


if __name__ == "__main__":
    main()
