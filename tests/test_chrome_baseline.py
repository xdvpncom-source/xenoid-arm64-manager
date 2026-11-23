"""Protect baseline validation and native creation/recreation command order."""

import contextlib
import hashlib
import io
import json
import os
import runpy
import stat
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


FLEET = runpy.run_path(str(Path(__file__).resolve().parents[1] / "xenoid-fleet"))
BASELINE = FLEET["ChromeBaseline"]
FLEET_GLOBALS = BASELINE.load.__func__.__globals__
BASE_IMAGE_ID = "sha256:" + "a" * 64
RUNTIME_IMAGE_ID = "sha256:" + "b" * 64
BOOT_INPUT = "c" * 64


class Instance:
    root = Path("/fixture/upstream")

    def __init__(self, existing=False, installed=None, installed_by_command=720418033, image="stock"):
        self.existing = existing
        self.installed = installed
        self.installed_by_command = installed_by_command
        self.image = image
        self.library_sha256 = None
        self.boot_input = BOOT_INPUT
        self.commands = []

    def registry(self):
        return {"1": {"ok": True}} if self.existing else {}

    def require(self, _device, command):
        self.commands.append(command)

    def recreate(self, _device):
        self.commands.append(["start", "--recreate", "--no-wait"])

    def document(self, command, _device):
        self.commands.append(command)
        if command == ["config", "show"]:
            return {"ok": True, "config": {"image": self.image, "docker_context": "qa-context",
                    "cold_profile": {"build": {"product": "bluejay", "model": "Pixel 6a"}},
                    "google_services_provider": "microg", "google_services_release": FLEET["BLUEJAY_GOOGLE_RELEASE"]}}, 0
        if command == ["status"]:
            return {
                "ok": True,
                "running": True,
                "runtimeIdentity": {"containerId": "native-container", "imageId": RUNTIME_IMAGE_ID},
                "runtimeImage": {"baseImageId": BASE_IMAGE_ID, "bootInputSha256": BOOT_INPUT},
                "runtimeImageIdentity": {"containerImageSha256": RUNTIME_IMAGE_ID,
                                         "containerBootInputSha256": self.boot_input,
                                         "desiredBootInputSha256": BOOT_INPUT},
            }, 0
        if command == ["adb", "shell", "sha256sum", FLEET["RUNTIME_LIBRARY"]]:
            return {"ok": True, "stdout": f"{self.library_sha256}  {FLEET['RUNTIME_LIBRARY']}\n"}, 0
        output = f"package:com.android.chrome versionCode:{self.installed}\n" if self.installed is not None else ""
        return {"ok": True, "stdout": output}, 0

    def call(self, command, **_options):
        self.commands.append(command)
        if command[:2] == ["adb", "install-multiple"] or (
            command[:2] == ["app", "install"] and Path(command[2]).name == "base.apk"
        ):
            self.installed = self.installed_by_command
        return CompletedProcess(command, 0, "", "")


def root_owned(info):
    data = list(info)
    data[4:6] = (0, 0)
    return os.stat_result(data)


class ChromeBaselineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "chrome-baseline"
        self.directory.mkdir(mode=0o700)
        self.enterContext(patch.dict(FLEET_GLOBALS, {"INSTALL_ROOT": self.root}))
        manifest = {
            "schema": "dev.xenoid.fleet.chrome-baseline/v1",
            "version_code": 720418033,
            "chrome": [self.apk("base.apk"), self.apk("split_config.en.apk")],
            "trichrome": self.apk("trichrome.apk"),
        }
        self.manifest = self.directory / "manifest.json"
        self.manifest.write_text(json.dumps(manifest))
        self.manifest.chmod(0o600)
        original_lstat, original_fstat = Path.lstat, os.fstat
        self.enterContext(patch.object(Path, "lstat", lambda path: root_owned(original_lstat(path))))
        self.enterContext(patch.object(os, "fstat", lambda fd: root_owned(original_fstat(fd))))

    def apk(self, filename):
        data = filename.encode()
        path = self.directory / filename
        path.write_bytes(data)
        path.chmod(0o600)
        return {"file": filename, "sha256": hashlib.sha256(data).hexdigest()}

    def test_create_and_recreate_install_after_native_up_and_profile(self):
        baseline = BASELINE.load()
        for existing in (False, True):
            with self.subTest(existing=existing), contextlib.redirect_stderr(io.StringIO()):
                instance = Instance(existing=existing)
                FLEET["create_device"](instance, "1", Namespace(no_google_services=False), "/profile.json", baseline)
                offset = int(existing)
                if existing:
                    self.assertEqual(instance.commands[0], ["config", "show"])
                self.assertEqual(instance.commands[offset][0], "start" if existing else "init")
                self.assertEqual(instance.commands[offset + 1:offset + 3], [["up"], ["device", "apply", "/profile.json", "--instance-identity"]])
                self.assertEqual(instance.commands[offset + 4], ["app", "install", str(self.directory / "trichrome.apk")])
                self.assertEqual(instance.commands[offset + 5], ["adb", "install-multiple", "-r", str(self.directory / "base.apk"),
                                                        str(self.directory / "split_config.en.apk")])
                self.assertEqual(instance.commands[offset + 3], instance.commands[offset + 6])

    def test_plain_up_converges_once_and_preserves_equal_or_later_versions(self):
        for version in (720418033, 720418034):
            instance = Instance(installed=version)
            with self.subTest(version=version), patch.dict(FLEET_GLOBALS, {"device_lock": lambda _device: contextlib.nullcontext()}):
                self.assertEqual(FLEET["run_devices"](instance, ["1"], ["up"]), 0)
            self.assertEqual(len(instance.commands), 2)
            self.assertEqual(instance.commands[0], ["up"])
            self.assertEqual(instance.commands[1][:4], ["adb", "shell", "pm", "list"])

    def test_mismatched_hash_and_insecure_modes_fail_before_device_routes(self):
        base = self.directory / "base.apk"
        base.chmod(0o644)
        with self.assertRaisesRegex(FLEET["FleetError"], "private root-owned"):
            BASELINE.load()
        base.chmod(0o600)
        base.write_bytes(b"changed")
        with self.assertRaisesRegex(FLEET["FleetError"], "SHA-256 mismatch"):
            BASELINE.load()

    def test_symlinked_baseline_is_rejected_and_missing_baseline_is_optional(self):
        replacement = self.root / "moved"
        self.directory.rename(replacement)
        self.assertIsNone(BASELINE.load())
        self.directory.symlink_to(replacement)
        self.assertTrue(stat.S_ISLNK(self.directory.lstat().st_mode))
        with self.assertRaisesRegex(FLEET["FleetError"], "private and root-owned"):
            BASELINE.load()

    def test_present_baseline_requires_a_valid_manifest_before_up(self):
        self.manifest.unlink()
        instance = Instance()
        with self.assertRaises(FileNotFoundError):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.manifest.write_text("invalid json")
        self.manifest.chmod(0o600)
        with self.assertRaisesRegex(FLEET["FleetError"], "invalid JSON"):
            FLEET["run_devices"](instance, ["1"], ["up"])
        self.assertEqual(instance.commands, [])

    def set_library_free_manifest(self, split_count):
        manifest = json.loads(self.manifest.read_text())
        manifest["chrome"] = manifest["chrome"][:split_count]
        manifest["trichrome"] = None
        self.manifest.write_text(json.dumps(manifest))
        (self.directory / "trichrome.apk").unlink(missing_ok=True)

    def runtime_manifest(self):
        self.set_library_free_manifest(1)
        manifest = json.loads(self.manifest.read_text())
        manifest["schema"] = FLEET["CHROME_RUNTIME_SCHEMA"]
        manifest["runtime"] = self.apk("libcutils.so")
        self.manifest.write_text(json.dumps(manifest))
        return manifest

    def test_v2_runtime_record_requires_exact_name_digest_modes_and_keys(self):
        manifest = self.runtime_manifest()
        self.assertEqual(BASELINE.load().runtime.sha256, manifest["runtime"]["sha256"])
        path = self.directory / "libcutils.so"
        path.chmod(0o644)
        with self.assertRaisesRegex(FLEET["FleetError"], "private root-owned"):
            BASELINE.load()
        path.chmod(0o600)
        path.write_bytes(b"different")
        with self.assertRaisesRegex(FLEET["FleetError"], "SHA-256 mismatch"):
            BASELINE.load()
        manifest["runtime"]["file"] = "../libcutils.so"
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(FLEET["FleetError"], "invalid runtime library record"):
            BASELINE.load()
        del manifest["runtime"]
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(FLEET["FleetError"], "expected schema"):
            BASELINE.load()

    def test_v2_builds_single_copy_over_exact_parent_and_reuses_checked_tag(self):
        self.runtime_manifest()
        runtime = BASELINE.load().runtime
        parent = FLEET["REDROID_BASE"]
        parent_layer = "sha256:" + "1" * 64
        current = {"Architecture": "arm64", "Os": "linux", "Id": parent.split("@")[1],
                   "RootFS": {"Layers": [parent_layer]}}
        derived = {
            **current, "Id": BASE_IMAGE_ID,
            "RootFS": {"Layers": [parent_layer, "sha256:" + "2" * 64]},
            "Config": {"Labels": {"dev.xenoid.fleet.chrome-parent": parent,
                                  "dev.xenoid.fleet.chrome-libcutils-sha256": runtime.sha256}},
        }
        commands = []

        def docker_call(context, command):
            self.assertEqual(context, "qa-context")
            commands.append(command)
            if command[0] == "build":
                self.assertIn("--network=none", command)
                self.assertIn("--pull=false", command)
                self.assertIn(runtime.image, command)
                stage = Path(command[-1])
                self.assertEqual({path.name for path in stage.iterdir()}, {"Dockerfile", "libcutils.so"})
                self.assertEqual((stage / "libcutils.so").read_bytes(), runtime.library.read_bytes())
                self.assertEqual((stage / "Dockerfile").read_text().splitlines(), [
                    f"FROM {parent}",
                    f"COPY --chown=0:0 --chmod=0644 libcutils.so {FLEET['RUNTIME_LIBRARY']}",
                ])
                return CompletedProcess(command, 0, "", "")
            if command[-1] == parent:
                return CompletedProcess(command, 0, json.dumps(current), "")
            self.assertEqual(command[-1], runtime.image)
            if not any(prior[0] == "build" for prior in commands):
                return CompletedProcess(command, 1, "", "No such image")
            return CompletedProcess(command, 0, json.dumps(derived), "")

        with patch.dict(FLEET_GLOBALS, {"docker_call": docker_call,
                                        "fleet_lock": lambda _name: contextlib.nullcontext()}):
            self.assertEqual(runtime.base_image_id("qa-context", build=True), BASE_IMAGE_ID)
            self.assertEqual(runtime.base_image_id("qa-context", build=True), BASE_IMAGE_ID)
            self.assertEqual(sum(command[0] == "build" for command in commands), 1)
            derived["RootFS"]["Layers"][0] = "sha256:" + "3" * 64
            with self.assertRaisesRegex(FLEET["FleetError"], "expected parent and library"):
                runtime.base_image_id("qa-context", build=True)

    def test_v2_only_explicit_create_changes_existing_image(self):
        self.runtime_manifest()
        baseline = BASELINE.load()
        for existing, image, leading in (
            (False, "stock", [["init", "--config", str(self.root / "defaults/bluejay-linux-arm.json"),
                               "--image", baseline.runtime.image]]),
            (True, "stock", [["config", "show"], ["config", "set", "--image", baseline.runtime.image]]),
            (True, baseline.runtime.image, [["config", "show"], ["start", "--recreate", "--no-wait"]]),
        ):
            with self.subTest(existing=existing, image=image), contextlib.redirect_stderr(io.StringIO()):
                instance = Instance(existing=existing, image=image)
                instance.library_sha256 = baseline.runtime.sha256
                with patch.dict(FLEET_GLOBALS, {"inspect_docker_image": lambda *_: {"Id": FLEET["REDROID_BASE"].split("@")[1]}}):
                    with patch.object(FLEET["ChromeRuntime"], "base_image_id", return_value=BASE_IMAGE_ID) as base:
                        FLEET["create_device"](instance, "1", Namespace(no_google_services=False), "/profile.json", baseline)
                base.assert_called_once_with("qa-context" if existing else "default", build=True)
                self.assertEqual(instance.commands[:len(leading)], leading)
                self.assertEqual(instance.commands[len(leading):len(leading) + 4], [
                    ["up"], ["device", "apply", "/profile.json", "--instance-identity"],
                    ["status"], ["adb", "shell", "sha256sum", FLEET["RUNTIME_LIBRARY"]],
                ])
                self.assertEqual(instance.commands.count(["up"]), 1)
                self.assertEqual(instance.commands[-2], ["app", "install", str(self.directory / "base.apk")])

    def test_v2_keeps_an_unrelated_configured_image(self):
        self.runtime_manifest()
        instance = Instance(existing=True, image="qa/other:1")
        with patch.dict(FLEET_GLOBALS, {"inspect_docker_image": lambda *_: {"Id": BASE_IMAGE_ID}}):
            with self.assertRaisesRegex(FLEET["FleetError"], "cannot replace a separately configured Android base"):
                FLEET["create_device"](instance, "1", Namespace(no_google_services=False), None, BASELINE.load())
        self.assertEqual(instance.commands, [["config", "show"]])

    def test_v2_plain_up_keeps_old_instances_and_checks_new_image_without_building(self):
        self.runtime_manifest()
        runtime = BASELINE.load().runtime
        with patch.dict(FLEET_GLOBALS, {"device_lock": lambda _device: contextlib.nullcontext()}):
            with patch.object(FLEET["ChromeRuntime"], "base_image_id", return_value=BASE_IMAGE_ID) as base:
                old = Instance(existing=True)
                self.assertEqual(FLEET["run_devices"](old, ["1"], ["up"]), 0)
                self.assertEqual(old.commands, [["up"], ["config", "show"]])
                base.assert_not_called()
                new = Instance(existing=True, installed=720418034, image=runtime.image)
                new.library_sha256 = runtime.sha256
                self.assertEqual(FLEET["run_devices"](new, ["1"], ["up"]), 0)
                base.assert_called_once_with("qa-context", build=False)
                self.assertEqual([command[0] for command in new.commands], ["up", "config", "status", "adb", "adb"])

    def test_v2_refuses_to_install_with_stale_runtime_or_wrong_library(self):
        self.runtime_manifest()
        baseline = BASELINE.load()
        stale = Instance()
        stale.boot_input = "d" * 64
        with self.assertRaisesRegex(FLEET["FleetError"], "runtime is not active"):
            baseline.converge(stale, "1", BASE_IMAGE_ID)
        self.assertEqual(stale.commands, [["status"]])
        wrong_library = Instance()
        wrong_library.library_sha256 = "e" * 64
        with self.assertRaisesRegex(FLEET["FleetError"], "running Android library does not match"):
            baseline.converge(wrong_library, "1", BASE_IMAGE_ID)
        self.assertEqual(wrong_library.commands, [
            ["status"], ["adb", "shell", "sha256sum", FLEET["RUNTIME_LIBRARY"]],
        ])

    def test_independent_single_apk_uses_native_app_install(self):
        self.set_library_free_manifest(1)
        baseline = BASELINE.load()
        instance = Instance()
        with contextlib.redirect_stderr(io.StringIO()):
            baseline.converge(instance, "1")
        self.assertEqual(instance.commands[1:-1], [["app", "install", str(self.directory / "base.apk")]])
        self.assertEqual(instance.commands[0], instance.commands[-1])

    def test_library_free_splits_use_one_native_install_multiple(self):
        self.set_library_free_manifest(2)
        baseline = BASELINE.load()
        instance = Instance()
        with contextlib.redirect_stderr(io.StringIO()):
            baseline.converge(instance, "1")
        self.assertEqual(instance.commands[1:-1], [["adb", "install-multiple", "-r", str(self.directory / "base.apk"),
                                                    str(self.directory / "split_config.en.apk")]])

    def test_independent_baseline_rejects_empty_package_or_unverified_install(self):
        self.set_library_free_manifest(1)
        baseline = BASELINE.load()
        with self.assertRaisesRegex(FLEET["FleetError"], "could not be verified"):
            baseline.converge(Instance(installed_by_command=None), "1")
        self.set_library_free_manifest(0)
        with self.assertRaisesRegex(FLEET["FleetError"], "expected schema"):
            BASELINE.load()


if __name__ == "__main__":
    unittest.main()
