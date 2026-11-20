"""Verify QA lifecycle placement and exact selected-device forwarding."""
import argparse
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FLEET = runpy.run_path(str(ROOT / "xenoid-fleet"))
G = FLEET["qa_reconcile"].__globals__


class QADispatchTest(unittest.TestCase):
    def test_proxy_upgrade_forwards_selected_devices_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'upgrade-proxy-control.py').touch()
            args = argparse.Namespace(action='qa', qa_action='upgrade-proxy', ids='2,81', timeout=0)
            with patch.dict(G, {'INSTALL_ROOT': root, 'Upstream': lambda: object(),
                                'selection': lambda _upstream, selected: ['2', '81']}), \
                    patch.object(subprocess, 'run') as call:
                call.return_value.returncode = 7
                self.assertEqual(FLEET['execute'](args, []), 7)
                self.assertEqual(call.call_args.args[0][2:],
                                 ['--timeout', '0', '--device', 'phone-2', '--device', 'phone-81'])

    def test_explicit_reconcile_requires_installed_addon(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(G, {"INSTALL_ROOT": Path(directory)}):
            with self.assertRaisesRegex(FLEET["FleetError"], "missing"):
                FLEET["qa_reconcile"](["2"], required=True)

    def test_reconcile_forwards_only_selected_native_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "panel-scripts" / "qa_resources.py"
            helper.parent.mkdir()
            helper.touch()
            with patch.dict(G, {"INSTALL_ROOT": root}), patch.object(subprocess, "run") as call:
                call.return_value.returncode = 0
                FLEET["qa_reconcile"](["2", "81"], force=True)
                self.assertEqual(call.call_args.args[0][2:],
                                 ["--force", "--device", "phone-2", "--device", "phone-81"])

    def test_failed_convergence_does_not_report_create_success(self):
        events = []
        class Upstream:
            def registry(self):
                return {}
            root = Path("/fixture/upstream")
            def require(self, device, command):
                events.append(command[0])
        def resource(ids, **kwargs):
            events.append("qa")
            raise FLEET["FleetError"]("pending")
        class Proxy:
            def converge(self, *_args):
                events.append("proxy")
        class Baseline:
            runtime = None
            def converge(self, *_args):
                events.append("chrome")
        with patch.dict(G, {"qa_reconcile": resource}):
            with self.assertRaisesRegex(FLEET["FleetError"], "pending"):
                FLEET["create_device"](Upstream(), "2", argparse.Namespace(no_google_services=False),
                                        "/fixture/profile.json", Baseline(), Proxy())
        self.assertEqual(events, ["init", "up", "device", "proxy", "chrome", "qa"])

    def test_up_option_and_waited_start_receive_hook_but_no_wait_does_not(self):
        class Upstream:
            def call(self, *_args, **_kwargs):
                return subprocess.CompletedProcess([], 0)
        from contextlib import nullcontext
        applied = []
        with patch.dict(G, {"qa_reconcile": lambda ids, **kw: applied.append(ids),
                            "device_lock": lambda _: nullcontext()}):
            self.assertEqual(FLEET["run_devices"](Upstream(), ["2"], ["up", "--skip-build"]), 0)
            self.assertEqual(FLEET["run_devices"](Upstream(), ["3"], ["start"]), 0)
            self.assertEqual(FLEET["run_devices"](Upstream(), ["4"], ["start", "--no-wait"]), 0)
            self.assertEqual(FLEET["run_devices"](Upstream(), ["5"], ["up", "--dry-run"]), 0)
            self.assertEqual(FLEET["run_devices"](Upstream(), ["6"], ["start", "--dry-run"]), 0)
        self.assertEqual(applied, [["2"], ["3"]])
