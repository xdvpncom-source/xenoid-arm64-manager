"""Source-owned, runtime-only reported GLES renderer overlay.

The software backend and all capabilities remain unchanged. The file binding is
owned by up and never changes boot image or regeneration identity inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

BASE_SHA256 = "8385182775899793a5831cd9535b07b8d993b8582e4c4bbbf58d06e828f5216d"
TARGET = "/vendor/lib64/egl/libGLESv2_angle.so"
ROOT = "/data/local/tmp/xenoid-renderer"
CONTROL = "/data/local/tmp/xenoid-renderer-control"
LIBRARY_OUTPUT = "native/xenoid-renderer/libGLESv2_angle.so"
MANIFEST_OUTPUT = "native/xenoid-renderer/renderer-label.json"
CONTROL_OUTPUT = "native/xenoid-renderer/xenoid-renderer"
RECORD_SCHEMA = "dev.xenoid.renderer-binding/v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")


def renderer_environment(cfg: Any) -> dict[str, str]:
    # Rollback still uses a valid candidate object to prove which mount we own.
    return {"XENOID_GLES_RENDERER": getattr(cfg, "graphics_renderer", "Mali-G78") or "Mali-G78"}


def _identity(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    dev, ino = value.get("device"), value.get("inode")
    if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in (dev, ino)):
        return None
    return dev, ino


def zygote_mapping_proof(output: str, device: int, inode: int) -> dict[str, Any]:
    """Require every currently reported zygote's real GLES mapping to match."""
    lines = output.splitlines()
    if len(lines) < 3 or not lines[0].startswith("BEGIN ") or not lines[-1].startswith("END "):
        return {"ok": False, "error": "graphics_zygote_proof_incomplete"}
    expected = lines[0][6:].split()
    if (not expected or len(expected) > 8 or any(not p.isdigit() or int(p) <= 0 for p in expected)
            or len(set(expected)) != len(expected) or lines[-1][4:].split() != expected):
        return {"ok": False, "error": "graphics_zygote_identity_invalid"}
    current: str | None = None
    observed: dict[str, set[tuple[int, int]]] = {}
    for line in lines[1:-1]:
        if line.startswith("PID "):
            current = line[4:]
            if current not in expected or current in observed:
                return {"ok": False, "error": "graphics_zygote_identity_invalid"}
            observed[current] = set()
            continue
        fields = line.split()
        if (current is None or len(fields) not in {6, 7} or fields[5] != TARGET
                or len(fields) == 7 and fields[6] != "(deleted)"):
            return {"ok": False, "error": "graphics_zygote_mapping_invalid"}
        try:
            major, minor = (int(part, 16) for part in fields[3].split(":"))
            mapped = os.makedev(major, minor), int(fields[4])
        except (ValueError, OverflowError):
            return {"ok": False, "error": "graphics_zygote_mapping_invalid"}
        observed[current].add(mapped)
    matches = (set(observed) == set(expected)
               and all(values == {(device, inode)} for values in observed.values()))
    return {"ok": matches, "zygoteCount": len(observed), "mapped": matches,
            **({} if matches else {"error": "graphics_restart_required"})}



class RendererOverlay:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def _root(self, command: str) -> dict[str, Any]:
        try:
            value = self.manager.daemon_client(timeout=15.0).root_exec(command)
        except Exception:
            return {"ok": False, "error": "graphics_root_unavailable"}
        # Public daemon RootHelper contract is `exit`; rootd internally differs.
        if not isinstance(value, Mapping) or value.get("ok") is not True or value.get("exit") != 0:
            return {"ok": False, "error": "graphics_root_command_failed"}
        return {"ok": True, "stdout": str(value.get("stdout") or "")}

    def _stat(self, path: str) -> dict[str, Any]:
        q = shlex.quote(path)
        value = self._root(f"test -f {q} && test ! -L {q} && stat -c '%u %g %a %h %d %i %s' {q} && sha256sum {q}")
        lines = str(value.get("stdout") or "").splitlines()
        if value.get("ok") is not True or len(lines) != 2:
            return {"ok": False, "error": "graphics_file_identity_invalid"}
        try:
            uid, gid, mode, links, dev, ino, size = lines[0].split()
            digest = lines[1].split()[0]
            result = {"ok": True, "uid": int(uid), "gid": int(gid), "mode": int(mode, 8),
                      "links": int(links), "device": int(dev), "inode": int(ino), "size": int(size), "sha256": digest}
            if not _SHA.fullmatch(digest) or _identity(result) is None:
                raise ValueError
            return result
        except (TypeError, ValueError, IndexError):
            return {"ok": False, "error": "graphics_file_identity_invalid"}

    def _record_path(self) -> Path:
        return self.manager.context.state_root / "renderer-binding-v1.json"

    def _read_record(self) -> dict[str, Any] | None:
        path = self._record_path()
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
                raise ValueError
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise ValueError("graphics_record_invalid")
        if (not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA
                or record.get("instanceId") != self.manager.context.instance_id
                or not _SHA.fullmatch(str(record.get("sourceSha256") or ""))
                or _identity(record.get("source")) is None or _identity(record.get("base")) is None
                or not _SHA.fullmatch(str(record.get("manifestSha256") or ""))):
            raise ValueError("graphics_record_invalid")
        return record

    def _write_record(self, record: Mapping[str, Any]) -> None:
        path = self._record_path()
        descriptor, name = tempfile.mkstemp(prefix=".renderer-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _artifacts(self, records: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        artifact, output, error = self.manager._pinned_artifact_object("glesRenderer", records, output_path=LIBRARY_OUTPUT)
        descriptor, descriptor_output, descriptor_error = self.manager._pinned_artifact_object("glesRenderer", records, output_path=MANIFEST_OUTPUT)
        if error or descriptor_error or artifact is None or descriptor is None or output is None or descriptor_output is None:
            raise ValueError("graphics_artifact_unavailable")
        manifest = json.loads(descriptor.read_text())
        label = getattr(self.manager.cfg, "graphics_renderer", "Mali-G78")
        if (not isinstance(manifest, dict) or manifest.get("schema") != "dev.xenoid.gles-renderer/v1"
                or manifest.get("original_sha256") != BASE_SHA256
                or manifest.get("output_sha256") != output.get("sha256")
                or manifest.get("output_size") != output.get("size")
                or manifest.get("reported_name_only") is not True
                or manifest.get("physical_gpu_changed") is not False
                or label is not None and manifest.get("label") != label):
            raise ValueError("graphics_manifest_invalid")
        return dict(output), dict(descriptor_output)

    def _verify_control(self, records: Any) -> None:
        artifact, output, error = self.manager._pinned_artifact_object("rendererControl", records, output_path=CONTROL_OUTPUT)
        actual = self._stat(CONTROL)
        if (error or artifact is None or not output or actual.get("sha256") != output.get("sha256")
                or actual.get("uid") != 0 or actual.get("gid") != 0
                or actual.get("mode") != 0o755 or actual.get("links") != 1):
            raise ValueError("graphics_control_identity_invalid")

    def _verify_manifest(self, record: Mapping[str, Any]) -> None:
        actual = self._stat(f"{ROOT}/objects/{record['sourceSha256']}/renderer-label.json")
        if (actual.get("sha256") != record["manifestSha256"] or actual.get("uid") != 0
                or actual.get("gid") != 0 or actual.get("mode") != 0o600 or actual.get("links") != 1):
            raise ValueError("graphics_manifest_identity_invalid")

    def _helper(self, action: str, record: Mapping[str, Any]) -> dict[str, Any]:
        source = f"{ROOT}/objects/{record['sourceSha256']}/libGLESv2_angle.so"
        args = [CONTROL, action, source, record["sourceSha256"],
                str(record["source"]["device"]), str(record["source"]["inode"]),
                str(record["base"]["device"]), str(record["base"]["inode"])]
        result = self._root(" ".join(shlex.quote(value) for value in args))
        try:
            value = json.loads(str(result.get("stdout") or ""))
        except ValueError:
            return {"ok": False, "error": "graphics_helper_response_invalid"}
        if (result.get("ok") is not True or not isinstance(value, dict)
                or value.get("schema") != "dev.xenoid.renderer-mount/v1" or value.get("ok") is not True
                or value.get("state") not in {"native", "bound"} or value.get("readonly") is not True):
            return {"ok": False, "error": "graphics_mount_unverified"}
        return value

    def _mappings(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        command = (
            "set -eu; zygotes() { (pidof zygote64 zygote 2>/dev/null || true) | "
            "tr ' ' '\\n' | sed '/^$/d' | sort -n | tr '\\n' ' '; }; "
            "pids=$(zygotes); test -n \"$pids\"; printf 'BEGIN %s\\n' \"$pids\"; "
            "for p in $pids; do printf 'PID %s\\n' \"$p\"; "
            f"awk '$6 == \"{TARGET}\" {{print}}' /proc/\"$p\"/maps; done; "
            "after=$(zygotes); test \"$after\" = \"$pids\"; printf 'END %s\\n' \"$after\""
        )
        result = self._root(command)
        if result.get("ok") is not True:
            return {"ok": False, "error": "graphics_zygote_unavailable"}
        return zygote_mapping_proof(str(result.get("stdout") or ""), int(identity["device"]), int(identity["inode"]))

    def observe(self, artifact_records: Any = None) -> dict[str, Any]:
        try:
            desired = getattr(self.manager.cfg, "graphics_renderer", "Mali-G78")
            record = self._read_record()
            target = self._stat(TARGET)
            if record is None:
                if desired is not None:
                    return {"ok": False, "state": "drift", "error": "graphics_not_configured"}
                if target.get("sha256") != BASE_SHA256:
                    raise ValueError("graphics_unowned_target")
                mappings = self._mappings(target)
                return {"ok": mappings.get("ok") is True, "state": "native", "reportedLabel": None, **mappings}
            if artifact_records is not None and desired is not None:
                output, descriptor = self._artifacts(artifact_records)
                if record["sourceSha256"] != output["sha256"] or record["manifestSha256"] != descriptor["sha256"]:
                    return {"ok": False, "state": "drift", "error": "graphics_artifact_drift"}
            self._verify_control(artifact_records)
            self._verify_manifest(record)
            status = self._helper("status", record)
            expected_state = "native" if desired is None else "bound"
            if status.get("ok") is not True or status.get("state") != expected_state:
                return {"ok": False, "state": "drift", "error": "graphics_mount_drift"}
            expected_identity = record["base"] if desired is None else record["source"]
            mappings = self._mappings(expected_identity)
            return {"ok": mappings.get("ok") is True, "state": status["state"],
                    "reportedLabel": desired, "sourceSha256": record["sourceSha256"],
                    "readonly": True, "owned": True, **mappings}
        except (OSError, ValueError, KeyError, TypeError) as error:
            code = str(error)
            return {"ok": False, "state": "drift", "error": code if re.fullmatch(r"graphics_[a-z_]+", code) else "graphics_observation_failed"}

    def converge(self, artifact_records: Any, expected_container_id: str | None = None) -> dict[str, Any]:
        container, _ = self.manager._owned_container_record()
        if not container or expected_container_id is not None and container.get("Id") != expected_container_id:
            return {"ok": False, "error": "convergence_state_conflict"}
        try:
            desired = getattr(self.manager.cfg, "graphics_renderer", "Mali-G78")
            record = self._read_record()
            target = self._stat(TARGET)
            self._verify_control(artifact_records)
            if desired is None and record is None:
                return self.observe(artifact_records)
            if record is not None:
                self._verify_manifest(record)
            if desired is None and target.get("sha256") == BASE_SHA256 and record is not None:
                source = self._stat(f"{ROOT}/objects/{record['sourceSha256']}/libGLESv2_angle.so")
                if source.get("sha256") != record["sourceSha256"]:
                    raise ValueError("graphics_source_unavailable")
                record = dict(record, containerId=container["Id"],
                              source={k: source[k] for k in ("device", "inode")},
                              base={k: target[k] for k in ("device", "inode")})
                self._write_record(record)
            if desired is not None:
                output, descriptor = self._artifacts(artifact_records)
                source_path = f"{ROOT}/objects/{output['sha256']}/libGLESv2_angle.so"
                source = self._stat(source_path)
                if source.get("sha256") != output["sha256"]:
                    raise ValueError("graphics_source_unavailable")
                if target.get("sha256") != BASE_SHA256:
                    if record is None:
                        raise ValueError("graphics_unowned_target")
                    # Label change: detach only the previous verified owned
                    # object, then bind the new immutable object before reboot.
                    if record["sourceSha256"] != output["sha256"]:
                        previous = self._helper("detach", record)
                        if previous.get("ok") is not True:
                            return previous
                        target = self._stat(TARGET)
                if target.get("sha256") == BASE_SHA256:
                    # Cold start: adopt fresh mount device numbers only after
                    # exact source and original target hashes are established.
                    record = {"schema": RECORD_SCHEMA, "instanceId": self.manager.context.instance_id,
                              "containerId": container["Id"], "sourceSha256": output["sha256"],
                              "manifestSha256": descriptor["sha256"],
                              "source": {k: source[k] for k in ("device", "inode")},
                              "base": {k: target[k] for k in ("device", "inode")}}
                    self._write_record(record)
                elif record is None or record["sourceSha256"] != output["sha256"]:
                    raise ValueError("graphics_unowned_target")
            status = self._helper("detach" if desired is None else "bind", record)
            if status.get("ok") is not True:
                return status
            identity = record["base"] if desired is None else record["source"]
            proof = self._mappings(identity)
            restarted = False
            if proof.get("ok") is not True:
                restart = self.manager.soft_reboot(require_health=False, reset_ssaid=False)
                if restart.get("ok") is not True:
                    details = {"ok": False}
                    for key in ("code", "error", "causeCode"):
                        code = restart.get(key)
                        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
                            details[key] = code
                    if restart.get("controlOp") in {"cellular-ready-netd", "cellular-ready-rules", "cellular-ready-addresses", "cellular-init-control-rules"}:
                        details["controlOp"] = restart["controlOp"]
                    for key, maximum in (("controlErrno", 4095), ("rootExitCode", 255)):
                        value = restart.get(key)
                        if type(value) is int and (0 if key == "controlErrno" else -255) <= value <= maximum:
                            details[key] = value
                    return {"ok": False, "error": "graphics_restart_failed",
                            "errorCode": details.get("code", details.get("error", "graphics_restart_failed")),
                            "softReboot": details}
                restarted = True
            after = self.observe(artifact_records)
            return {**after, "restarted": restarted, "changed": status.get("changed") is True}
        except (OSError, ValueError, KeyError, TypeError) as error:
            code = str(error)
            return {"ok": False, "error": code if re.fullmatch(r"graphics_[a-z_]+", code) else "graphics_convergence_failed"}
