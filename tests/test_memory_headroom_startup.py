"""Run the real native start method with isolated lifecycle collaborators."""
import ast
import contextlib
import importlib.util
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock
from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "nativebundle/source/src/xenoid/backend.py"
CID = "a" * 64


def methods():
    tree = ast.parse(SOURCE.read_text())
    owner = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "RuntimeManager")
    names = {"_qa_memory_headroom", "start_owned_container"}
    nodes = [item for item in owner.body if isinstance(item, ast.FunctionDef) and item.name in names]
    assert len(nodes) == 2
    namespace = {"Mapping": Mapping, "re": re, "shlex": shlex, "stat": stat, "Path": Path}
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(program), str(SOURCE), "exec"), namespace)
    return namespace


class NativeMemoryStartup(unittest.TestCase):
    def fixture(self, running=False, failure=False):
        trace = []
        container = {"Id": CID, "Image": "sha256:" + "b" * 64, "State": {"Running": running}}
        def start(command, *, timeout):
            self.assertEqual(timeout, 60)
            self.assertIn("docker start " + CID, command)
            trace.append("docker_start")
            container["State"]["Running"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        def headroom(cid):
            self.assertEqual(cid, CID)
            self.assertIs(container["State"]["Running"], True)
            trace.append("memory_policy")
            return {"ok": not failure, "error": "headroom_systemd_pending_ack_unknown"} if failure else {"ok": True}
        manager = types.SimpleNamespace(
            ensure_instance_lease=lambda: None,
            _owned_container_record=lambda: (container, None),
            _container_matches_lease=lambda *args, **kwargs: True,
            _qa_host_vm_isolation=lambda cid: trace.append("producer_isolation") or {"ok": True},
            proxy_bootstrap_quarantine=lambda: {"ok": True},
            _shared_protection_engine_lock=contextlib.nullcontext,
            shared_protection_manager=lambda: types.SimpleNamespace(status=lambda: {"ok": True}),
            _shared_protection_engine_shell=Mock(side_effect=start),
            _qa_memory_headroom=Mock(side_effect=headroom),
            _republish_statfs_fsid_after_start=lambda result: trace.append("statfs_replay"))
        return manager, container, trace

    def test_new_start_applies_memory_before_readiness_and_wait_false_return(self):
        manager, container, trace = self.fixture()
        result = methods()["start_owned_container"](manager, expected_container_id=CID, wait=False)
        self.assertTrue(result["ok"])
        self.assertEqual(trace, ["producer_isolation", "docker_start", "memory_policy", "statfs_replay"])
        self.assertTrue(result["memoryHeadroom"]["ok"])
        self.assertEqual(manager._shared_protection_engine_shell.call_count, 1)

    def test_already_running_runtime_also_persists_policy_without_docker_start(self):
        manager, container, trace = self.fixture(running=True)
        self.assertTrue(methods()["start_owned_container"](manager, expected_container_id=CID, wait=False)["ok"])
        self.assertEqual(trace, ["producer_isolation", "memory_policy", "statfs_replay"])
        manager._shared_protection_engine_shell.assert_not_called()

    def test_unknown_policy_ack_fails_readiness_preserves_running_container_and_resumes_once(self):
        manager, container, trace = self.fixture(failure=True)
        start = methods()["start_owned_container"]
        result = start(manager, expected_container_id=CID, wait=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "headroom_systemd_pending_ack_unknown")
        self.assertNotIn("statfs_replay", trace)
        self.assertTrue(container["State"]["Running"])
        manager._qa_memory_headroom.side_effect = lambda cid: {"ok": True, "changed": False}
        self.assertTrue(start(manager, expected_container_id=CID, wait=False)["ok"])
        self.assertEqual(manager._shared_protection_engine_shell.call_count, 1, "Resume must not replay Docker start")

    def test_wrong_owned_container_rejects_before_any_policy_or_start(self):
        manager, container, trace = self.fixture()
        result = methods()["start_owned_container"](manager, expected_container_id="c" * 64, wait=False)
        self.assertFalse(result["ok"])
        manager._qa_memory_headroom.assert_not_called()
        manager._shared_protection_engine_shell.assert_not_called()
        self.assertEqual(trace, [])


class NativeMemoryLoader(unittest.TestCase):
    def fixture(self, path):
        namespace = methods()
        namespace["Path"] = lambda value: path
        depth = []
        @contextlib.contextmanager
        def lock(*, timeout_seconds):
            self.assertEqual(timeout_seconds, 30)
            depth.append(True)
            try:
                yield
            finally:
                depth.pop()
        manager = types.SimpleNamespace(instance_operation_lock=lock, depth=depth)
        return namespace["_qa_memory_headroom"], manager

    def test_optional_addon_absence_preserves_standalone_native_start(self):
        method, manager = self.fixture(types.SimpleNamespace(exists=lambda: False))
        self.assertEqual(method(manager, CID), {"ok": True, "skipped": "qa_memory_addon_not_installed"})

    def test_untrusted_helper_is_never_executed(self):
        for changes in ({"st_uid": 1000}, {"st_nlink": 2}, {"st_mode": stat.S_IFREG | 0o777},
                        {"st_mode": stat.S_IFLNK | 0o777}):
            with self.subTest(changes=changes):
                info = {"st_uid": 0, "st_nlink": 1, "st_mode": stat.S_IFREG | 0o755, **changes}
                path = types.SimpleNamespace(exists=lambda: True, lstat=lambda: types.SimpleNamespace(**info))
                method, manager = self.fixture(path)
                self.assertEqual(method(manager, CID), {"ok": False, "error": "qa_memory_helper_invalid"})

    def test_trusted_helper_runs_under_native_lock_and_exception_text_is_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture_memory_helper.py"
            class FixturePath:
                # Metadata is an explicit permission-boundary double. The
                # helper Python body is really loaded from this private file.
                def exists(self): return True
                def lstat(self): return types.SimpleNamespace(st_uid=0, st_nlink=1, st_mode=stat.S_IFREG | 0o755)
                def __fspath__(self): return str(path)
            path.write_text('def ensure_configured_memory_headroom(manager, cid):\n'
                            '    assert manager.depth == [True]\n'
                            '    manager.seen = cid\n'
                            '    return {"ok": True, "changed": False}\n')
            method, manager = self.fixture(FixturePath())
            self.assertTrue(method(manager, CID)["ok"])
            self.assertEqual(manager.seen, CID)
            self.assertEqual(manager.depth, [])
            path.write_text('def ensure_configured_memory_headroom(manager, cid):\n'
                            '    raise RuntimeError("fixture-private://account:token@example.invalid")\n')
            self.assertEqual(method(manager, CID), {"ok": False, "error": "qa_memory_headroom_failed"})


if __name__ == "__main__":
    unittest.main()
