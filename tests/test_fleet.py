"""Exercise fleet dispatch without Docker, devices, or a real registry."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MANAGER = Path(__file__).resolve().parents[1] / "xenoid-fleet"
STUB = '''\
#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
assert "XENOID_EXPECT_INSTANCE_ID" not in os.environ
assert "XENOID_INSTANCE" not in os.environ
record = {"args": args, "cwd": os.getcwd(), "home": os.environ["HOME"],
          "project": os.environ["XENOID_PROJECT"]}
if len(args) > 4 and args[2:4] == ["device", "apply"]:
    record["profile"] = json.loads(Path(args[4]).read_text())
if len(args) > 2 and args[2] == "up":
    record["buildx_config"] = os.environ.get("BUILDX_CONFIG")
    assert Path(record["buildx_config"]).is_dir()
with Path(os.environ["FLEET_CALLS"]).open("a") as log:
    log.write(json.dumps(record) + "\\n")

registry = Path(os.environ["FLEET_REGISTRY"])
instances = json.loads(registry.read_text())
if args == ["instance", "list"]:
    print(json.dumps({"ok": True, "instances": instances}))
    sys.exit(0)

assert args[0] == "--instance"
name, command = args[1], args[2:]
if os.environ.get("FLEET_FAIL") == f"{name}:{command[0]}":
    print(json.dumps({"ok": False, "error": "test failure"}))
    sys.exit(27)
if command == ["start", "--recreate", "--no-wait"] and "FLEET_RECOVER" in os.environ:
    marker = registry.with_name("recreate-attempted")
    if not marker.exists():
        marker.touch()
        print(json.dumps({"ok": False, "error": os.environ["FLEET_RECOVER"]}))
        sys.exit(1)
if command[0] == "init":
    assert not any(row["instanceName"] == name for row in instances)
    time.sleep(0.1)
    instances.append({"instanceName": name, "slot": len(instances), "state": "active"})
    registry.write_text(json.dumps(instances))
if command == ["status"]:
    ok = os.environ.get("FLEET_UNHEALTHY") != name
    print(json.dumps({"ok": ok, "running": name != "phone-2", "recommendedAction": "no-op"}))
    sys.exit(0 if ok else 1)
elif command == ["config", "show"]:
    config = {"google_services_provider": "microg",
              "google_services_release": "microg-0.3.15.250932-phonesky-30.4.17-gsfproxy-0.1.0-bluejay",
              "cold_profile": {"build": {"product": "bluejay", "model": "Pixel 6a"}}}
    if os.environ.get("FLEET_LEGACY"):
        config.pop("cold_profile")
    print(json.dumps({"ok": True, "config": config}))
else:
    print(json.dumps({"ok": True, "args": command}))
'''


class FleetTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "operator home"
        self.home.mkdir()
        self.upstream = self.root / "checkout with spaces"
        self.upstream.mkdir()
        launcher = self.upstream / "xenoid"
        launcher.write_text(STUB)
        launcher.chmod(0o755)
        scripts = self.upstream / "scripts"
        scripts.mkdir()
        (scripts / "import-pixel-template.py").write_text(
            'def normalize(data, name):\n'
            '    if data.get("schema") != "fixture-profile":\n'
            '        raise ValueError("invalid fixture profile")\n'
            '    return data\n'
        )
        self.cwd = self.root / "caller directory"
        self.cwd.mkdir()
        self.registry = self.root / "instances.json"
        self.set_instances()
        self.log = self.root / "calls.jsonl"
        self.env = dict(os.environ, HOME=str(self.home), XENOID_FLEET_UPSTREAM=str(self.upstream),
                        XENOID_PROJECT="/incorrect/project", FLEET_CALLS=str(self.log),
                        FLEET_REGISTRY=str(self.registry), XENOID_INSTANCE="stale-instance",
                        XENOID_EXPECT_INSTANCE_ID="stale-id")
        self.env.pop("BUILDX_CONFIG", None)
        self.env.pop("BUILDX_BUILDER", None)

    def set_instances(self, *names: str) -> None:
        self.registry.write_text(json.dumps([
            {"instanceName": name, "slot": slot, "state": "active"}
            for slot, name in enumerate(names)
        ]))

    def call(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=self.cwd,
                              env=self.env, capture_output=True, text=True, timeout=10)

    def records(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        return [record["args"] for record in self.records()]

    def test_single_target_preserves_cwd_home_and_exact_arguments(self) -> None:
        command = ["adb", "shell", "", "space and ' apostrophe", "$(touch SHOULD_NOT_EXIST)"]
        result = self.call("run", "7", "--", *command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["args"], command)
        self.assertEqual(self.records(), [{"args": ["--instance", "phone-7", *command],
                                         "cwd": str(self.cwd), "home": str(self.home),
                                         "project": str(self.upstream)}])
        self.assertFalse((self.cwd / "SHOULD_NOT_EXIST").exists())

    def test_service_without_home_uses_operator_home(self) -> None:
        self.env["HOME"] = ""
        result = self.call("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[0]["home"], pwd.getpwuid(os.getuid()).pw_dir)

    def test_image_builds_use_separate_buildx_client_state(self) -> None:
        for device_id in ("1", "2"):
            result = self.call("run", device_id, "--", "up")
            self.assertEqual(result.returncode, 0, result.stderr)
        configs = [Path(record["buildx_config"]) for record in self.records()]
        self.assertNotEqual(configs[0], configs[1])
        self.assertFalse(any(config.exists() for config in configs))

    def test_bulk_run_keeps_order_and_continues_after_error(self) -> None:
        self.env["FLEET_FAIL"] = "phone-8:input"
        command = ["input", "tap", "540", "1800"]
        result = self.call("run", "8,2,3", "--", *command)
        self.assertEqual(result.returncode, 1)
        self.assertIn("phone-8: upstream exited with status 27", result.stderr)
        self.assertEqual(self.calls(), [["--instance", f"phone-{number}", *command]
                                         for number in (8, 2, 3)])

    def test_single_run_preserves_upstream_failure_code(self) -> None:
        self.env["FLEET_FAIL"] = "phone-2:status"
        self.assertEqual(self.call("run", "2", "--", "status").returncode, 27)

    def test_invalid_ids_and_escaping_scope_do_not_call_upstream(self) -> None:
        for bundle in (
            ("0", "--", "status"),
            ("02", "--", "status"),
            ("1,1", "--", "status"),
            ("1", "status"),
            ("1", "--", "--instance", "different", "status"),
            ("1", "--", "delete"),
            ("1", "--", "instance", "list"),
            ("1", "--", "adb", "-s", "other", "shell"),
            ("1,2", "--", "view"),
        ):
            with self.subTest(bundle=bundle):
                self.assertNotEqual(self.call("run", *bundle).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_registry_list_is_numeric_and_includes_runtime_status(self) -> None:
        self.set_instances("phone-11", "default", "phone-2", "phone-03", "qa")
        result = self.call("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        devices = json.loads(result.stdout)["devices"]
        self.assertEqual([item["id"] for item in devices], [2, 11])
        self.assertEqual([item["status"]["running"] for item in devices], [False, True])

    def test_list_shows_running_even_when_health_is_unavailable(self) -> None:
        self.set_instances("phone-3")
        self.env["FLEET_UNHEALTHY"] = "phone-3"
        result = self.call("list")
        self.assertEqual(result.returncode, 1)
        self.assertIn("3\tphone-3\trunning\t", result.stdout)

    def test_create_new_or_recreate_preserving_profile_and_arguments(self) -> None:
        self.set_instances("phone-2")
        profile_data = {"schema": "fixture-profile", "value": "tests only"}
        (self.cwd / "test profile.json").write_text(json.dumps(profile_data))
        result = self.call("create", "1,2", "test profile.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[1], ["--instance", "phone-1", "init", "--config",
                                    str(MANAGER.parent / "defaults/bluejay-linux-arm.json")])
        self.assertEqual(calls[2], ["--instance", "phone-1", "up"])
        self.assertEqual(calls[5], ["--instance", "phone-2", "config", "show"])
        self.assertEqual(calls[6:8], [["--instance", "phone-2", "start", "--recreate", "--no-wait"],
                                      ["--instance", "phone-2", "up"]])
        for index in (3, 8):
            self.assertEqual(calls[index][2:4], ["device", "apply"])
            self.assertEqual(calls[index][-1], "--instance-identity")
            self.assertEqual(self.records()[index]["profile"], profile_data)
            self.assertFalse(Path(calls[index][4]).exists())

    def test_invalid_profile_fails_before_any_upstream_operation(self) -> None:
        (self.cwd / "profile.json").write_text('{"schema":"wrong"}')
        result = self.call("create", "1,2", "profile.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile validation failed", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_recreate_recovers_only_selected_upstream_codes(self) -> None:
        self.set_instances("phone-2")
        for code in (
            "runtime_image_required", "convergence_pending", "device_regeneration_pending",
            "google_services_assets_missing", "runtime_image_artifact_snapshot_invalid",
            "runtime_preflight_failed", "resource_conflict",
        ):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                self.registry.with_name("recreate-attempted").unlink(missing_ok=True)
                self.env["FLEET_RECOVER"] = code
                result = self.call("create", "2")
                commands = [args[2:] for args in self.calls() if args[:2] == ["--instance", "phone-2"]]
                if code == "resource_conflict":
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(commands, [["config", "show"], ["start", "--recreate", "--no-wait"]])
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recreate = ["start", "--recreate", "--no-wait"]
                    self.assertEqual(commands, [["config", "show"], recreate, ["up"], recreate, ["up"]])

    def test_legacy_profile_requires_explicit_delete_without_mutating_device(self):
        self.set_instances("phone-2")
        self.env["FLEET_LEGACY"] = "1"
        result = self.call("create", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("explicitly delete", result.stderr)
        self.assertEqual(self.calls(), [["instance", "list"], ["--instance", "phone-2", "config", "show"]])

    def test_provider_change_refused_for_existing_instance(self) -> None:
        self.set_instances("phone-2")
        result = self.call("create", "2", "--no-google-services")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [["instance", "list"], ["--instance", "phone-2", "config", "show"]])
        result = self.call("create", "3", "--no-google-services")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-2][-1], "--no-google-services")

    def test_delete_defaults_to_preview_and_all_is_scoped(self) -> None:
        self.set_instances("phone-9", "default", "phone-3")
        result = self.call("delete", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [["instance", "list"],
                                       ["--instance", "phone-3", "delete", "--dry-run"],
                                       ["--instance", "phone-9", "delete", "--dry-run"]])
        result = self.call("delete", "9", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], ["--instance", "phone-9", "delete"])

    def test_same_id_create_lifecycle_is_serialized(self) -> None:
        argv = [sys.executable, str(MANAGER), "create", "5"]
        with subprocess.Popen(argv, cwd=self.cwd, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as first:
            second = self.call("create", "5")
            _, first_stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        phases = [args[2:] for args in self.calls() if args[:2] == ["--instance", "phone-5"]]
        self.assertEqual([phase[0] for phase in phases], ["init", "up", "config", "start", "up"])


if __name__ == "__main__":
    unittest.main()
