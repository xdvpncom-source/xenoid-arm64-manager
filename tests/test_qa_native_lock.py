"""Real flock/ContextVar integration with the deployed, pinned native lock.

Only the native InstanceError dependency is replaced. The lock implementation
below was read without modifications from host 1's pinned upstream checkout.
"""
import contextlib
import hashlib
import importlib.util
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

NATIVE_SOURCE = r'''from __future__ import annotations

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional

from .config import InstanceError


EXPECTED_INSTANCE_ID_ENV = "XENOID_EXPECT_INSTANCE_ID"
OPERATION_LOCK_TIMEOUT_ENV = "XENOID_OPERATION_LOCK_TIMEOUT"

_HELD_INSTANCE_IDS: ContextVar[frozenset[str]] = ContextVar(
    "xenoid_held_instance_ids",
    default=frozenset(),
)


@contextmanager
def instance_operation_lock(
    state_root: Path,
    *,
    timeout_seconds: Optional[float] = None,
) -> Iterator[None]:
    """Serialize lifecycle/state mutation for one immutable instance.

    A `None` timeout is the trusted-local blocking behavior. Network workers use
    zero so a busy instance cannot consume a global request slot while queued.
    """

    root = Path(state_root)
    instance_id = root.name
    if instance_id in _HELD_INSTANCE_IDS.get():
        yield
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "operation.lock"
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InstanceError("instance_busy", "instance operation lock unavailable") from exc
    locked = False
    held_token = None
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise InstanceError("instance_busy", "instance operation lock invalid")
        if timeout_seconds is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        else:
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise InstanceError("instance_busy", "instance operation in progress") from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        held = _HELD_INSTANCE_IDS.get()
        held_token = _HELD_INSTANCE_IDS.set(held | {instance_id})
        yield
    finally:
        if held_token is not None:
            _HELD_INSTANCE_IDS.reset(held_token)
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def operation_lock_is_held(instance_id: str) -> bool:
    return (
        isinstance(instance_id, str)
        and bool(instance_id)
        and instance_id in _HELD_INSTANCE_IDS.get()
    )
'''
NATIVE_SHA256 = "9e469ec947782bbce398e23b6c9d249afafdab12f111be8a188970c01c404c3e"


class InstanceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_native_lock():
    package = types.ModuleType("qa_native_lock_fixture")
    package.__path__ = []
    config = types.ModuleType("qa_native_lock_fixture.config")
    config.InstanceError = InstanceError
    module = types.ModuleType("qa_native_lock_fixture.operation_lock")
    module.__package__ = "qa_native_lock_fixture"
    with patch.dict(sys.modules, {package.__name__: package, config.__name__: config}):
        exec(compile(NATIVE_SOURCE, "pinned-native-operation_lock.py", "exec"), module.__dict__)
    return module


NATIVE = load_native_lock()
ROOT = Path(__file__).resolve().parents[1]
QA = runpy.run_path(str(ROOT / "qa" / "qa_resources.py"))
RECONCILE = QA["reconcile"]
GLOBALS = RECONCILE.__globals__
# Use the release's actual Broker.owned_manager rather than imitating its inner
# six-second lock or runtime binding checks.
with patch.object(sys, "path", [str(ROOT / "qa"), *sys.path]):
    spec = importlib.util.spec_from_file_location("qa_native_real_script_host", ROOT / "qa" / "script_host.py")
    HOST = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(HOST)


class NativeManager:
    def __init__(self, root):
        self.context = types.SimpleNamespace(state_root=root, instance_id=root.name, resource_tag="0123456789ab")
        self.requests = []
        self.acquiring = threading.Event()
        self.owned = {"Id": "a" * 64, "State": {"Running": True, "StartedAt": "boot-1"}}

    @contextlib.contextmanager
    def instance_operation_lock(self, *, timeout_seconds=None):
        self.requests.append(timeout_seconds)
        self.acquiring.set()
        with NATIVE.instance_operation_lock(self.context.state_root, timeout_seconds=timeout_seconds):
            yield

    def _owned_container_record(self, timeout):
        return self.owned, None


class NativeLockIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = NativeManager(self.root / "immutable-instance-1")
        self.actions = []
        actions = self.actions

        class Broker(HOST.Broker):
            def set_frame_rate(self, fps, timeout=None):
                actions.append("fps_entered")
                with self.owned_manager():
                    actions.append("fps_under_native_lock")
                    return {"changed": False}

        def metrics(broker, *_args, **_kwargs):
            actions.append("metrics_entered")
            with broker.owned_manager():
                actions.append("metrics_under_native_lock")
            return {"changed": False}

        self.patch_globals = patch.dict(GLOBALS, {"STATE": self.root / "qa-state",
            "native_manager": lambda _name: self.manager, "apply_metrics": metrics})
        self.patch_globals.start()
        self.addCleanup(self.patch_globals.stop)
        self.patch_broker = patch.dict(sys.modules, {"script_host": types.SimpleNamespace(Broker=Broker)})
        self.patch_broker.start()
        self.addCleanup(self.patch_broker.stop)

    def test_fixture_matches_the_live_native_source(self):
        self.assertEqual(hashlib.sha256(NATIVE_SOURCE.encode()).hexdigest(), NATIVE_SHA256)

    def held_by_other_thread(self):
        acquired, release = threading.Event(), threading.Event()
        def holder():
            with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=None):
                acquired.set()
                release.wait(5)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(acquired.wait(2))
        self.addCleanup(thread.join, 2)
        self.addCleanup(release.set)
        return thread, release

    def assert_profile_waits_before_any_resource_work(self, timeout, expected_native_timeout):
        holder, release = self.held_by_other_thread()
        results, errors = [], []
        def run():
            try:
                results.append(RECONCILE("phone-1", QA["validate_config"]({"timeout": timeout}), force=True))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(self.manager.acquiring.wait(2))
        self.assertEqual(self.manager.requests, [expected_native_timeout])
        self.assertEqual(self.actions, [])
        self.assertTrue(worker.is_alive())
        release.set()
        worker.join(3)
        holder.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.manager.requests, [expected_native_timeout, 6, 6])
        self.assertEqual(self.actions, ["fps_entered", "fps_under_native_lock", "metrics_entered", "metrics_under_native_lock"])
        self.assertFalse(NATIVE.operation_lock_is_held(self.manager.context.instance_id))

    def test_admin_180_seconds_waits_then_reenters_real_broker_inner_lock(self):
        self.assert_profile_waits_before_any_resource_work(180, 180)

    def test_admin_zero_uses_native_none_blocking_then_reenters_real_broker(self):
        self.assert_profile_waits_before_any_resource_work(0, None)

    def test_native_finite_timeout_fails_before_resource_mutation(self):
        holder, release = self.held_by_other_thread()
        started = time.monotonic()
        with self.assertRaises(InstanceError) as caught:
            with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=.08):
                self.fail("The competing native lock must not be acquired")
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "instance_busy")
        self.assertGreaterEqual(elapsed, .075)
        self.assertLess(elapsed, .5)
        self.assertEqual(self.actions, [])
        release.set()
        holder.join(2)

    def test_inner_release_keeps_outer_lock_and_exception_releases_it(self):
        failures = []
        def compete():
            try:
                with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=0):
                    failures.append("acquired")
            except InstanceError as error:
                failures.append(error.code)
        with self.assertRaisesRegex(RuntimeError, "intentional"):
            with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=180):
                with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=6):
                    self.assertTrue(NATIVE.operation_lock_is_held(self.manager.context.instance_id))
                thread = threading.Thread(target=compete)
                thread.start()
                thread.join(2)
                self.assertEqual(failures, ["instance_busy"])
                raise RuntimeError("intentional")
        self.assertFalse(NATIVE.operation_lock_is_held(self.manager.context.instance_id))
        with NATIVE.instance_operation_lock(self.manager.context.state_root, timeout_seconds=0):
            self.assertTrue(NATIVE.operation_lock_is_held(self.manager.context.instance_id))


if __name__ == "__main__":
    unittest.main()
