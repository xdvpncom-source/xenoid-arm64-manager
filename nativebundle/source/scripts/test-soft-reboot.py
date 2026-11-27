#!/usr/bin/env python3
"""Exercise safe keystore/framework restart ordering without Android mutation."""
import ast
import json
from copy import deepcopy
from pathlib import Path
import subprocess
import re
import shlex
from types import MethodType, SimpleNamespace
from typing import Any, Optional
import unittest

import textwrap

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/xenoid/backend.py"
tree = ast.parse(SOURCE.read_text())
owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RuntimeManager")
method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "soft_reboot")
CANDIDATE = textwrap.dedent("\n".join(SOURCE.read_text().splitlines()[method.lineno - 1:method.end_lineno]))


class Clock:
    def __init__(self): self.value = 0.0
    def monotonic(self): self.value += .01; return self.value
    def sleep(self, seconds): self.value += max(0, seconds)


def namespace(clock):
    return {"Optional": Optional, "Any": Any, "re": re, "shlex": shlex, "json": json, "time": clock,
            "_SOFT_REBOOT_TIMEOUT_SECONDS": 180,
            "_REGENERATION_RESTART_RECEIPT": "/data/local/tmp/xenoid-device-regeneration.restart",
            "bounded_timeout": lambda value: None}


def parser():
    method = ast.parse(CANDIDATE).body[0]
    helper = next(node for node in method.body if isinstance(node, ast.FunctionDef) and node.name == "maintenance_state")
    env = {"Optional": Optional, "re": re}
    exec(compile(ast.Module(body=[deepcopy(helper)], type_ignores=[]), "<maintenance-parser>", "exec"), env)
    return env["maintenance_state"]


class Probe:
    def __init__(self, *, resume=False, maintenance=True, state=1, old_pid=False, stop_ready=True, netd_reset=True, control_rules=3, old_netd_pid=False, quarantine=True, feature_absent=False, cellular=True):
        self.clock = Clock()
        env = namespace(self.clock)
        exec(compile(CANDIDATE, "<keystore-order-candidate>", "exec"), env)
        self.soft_reboot = MethodType(env["soft_reboot"], self)
        self.lease = SimpleNamespace(rootd_port=42870)
        self.cfg = SimpleNamespace(cold_profile=None)
        self.events = []
        self.started = resume
        self.stopped = False
        self.keystore_pid = "20" if resume else "10"
        self.netd_pid = "40" if resume else "30"
        self.netd_reset, self.control_rules = netd_reset, control_rules
        self.old_netd_pid=old_netd_pid
        self.maintenance, self.state, self.old_pid, self.stop_ready = maintenance, state, old_pid, stop_ready
        self.receipt_ok = True
        self.quarantine_ok, self.feature_absent, self.cellular = quarantine, feature_absent, cellular
        self.address_restored = False

    def daemon_client(self, **kwargs):
        return SimpleNamespace(read_private_token=lambda **kw: "token", health=lambda **kw: {"ok": True})

    def ensure_rootd_root(self, *args): return {"ok": True}
    def reconcile_bootstrap(self, *args): self.events.append("bootstrap"); return {"controlReady": True}
    def reconcile_proxy_desired(self): return {"ok": True}
    def regeneration_restart_receipt(self, value): return {"ok": self.receipt_ok}
    def quarantine_proxy_for_lifecycle(self, *, deadline):
        self.events.append("lease-quarantine")
        assert self.stopped and not self.started and self.netd_pid == "40"
        if self.feature_absent:return {"ok":True,"notPrepared":True}
        self.address_restored = self.quarantine_ok
        return {"ok":self.quarantine_ok,"code":"ipv6_unsupported"} if not self.quarantine_ok else {"ok":True}

    def _rootd_output(self, args, **kwargs):
        self.events.append(tuple(args))
        if args == ["pidof", "keystore2"]: return self.keystore_pid
        if args == ["pidof", "netd"]: return self.netd_pid
        if args == ["getprop", "init.svc.keystore2"]: return "running"
        if args == ["getprop", "init.svc.netd"]: return "running"
        if args == ["getprop", "sys.boot_completed"]: return "1" if self.started else ""
        raise AssertionError(args)

    def _rootd_exec(self, command, token, **kwargs):
        self.events.append(command)
        if "ctl.stop zygote" in command:
            self.stopped = True
            self.started = False
        if command.startswith('test "$(getprop init.svc.zygote)"'):
            return {"ok": self.stopped and self.stop_ready, "exitCode": 0 if self.stop_ready else 1}
        if "setprop ctl.stop netd" in command:
            assert self.stopped and not self.started
            assert "setprop ctl.start netd" in command
            assert "ctl.restart netd" not in command
            assert "/system/bin/xenoid-netctl cellular-ready" not in command
            if not self.old_netd_pid:self.netd_pid="40"
            return {"ok":self.netd_reset,"exitCode":0 if self.netd_reset else 91,"stdout":""}
        if command == "/system/bin/xenoid-netctl cellular-ready":
            assert self.stopped and not self.started and self.address_restored
            if not self.cellular:
                return {"ok":True,"exitCode":1,"stdout":json.dumps({"ok":False,"op":"cellular-ready-addresses","errno":110,"private":"not-public"})}
            return {"ok":True,"exitCode":0,"stdout":json.dumps({"ok":True,"op":"cellular-init","ifname":"rmnet_data0","controlRules":self.control_rules})}
        if command == "setprop ctl.restart keystore2":
            assert self.stopped and not self.started
            if not self.old_pid: self.keystore_pid = "20"
        if command == "setprop ctl.restart vendor.ril-daemon;setprop ctl.start zygote":
            assert self.stopped
            self.started = True
            self.stopped = False
        if command.startswith("service check "):
            service = command[len("service check "):]
            found = self.maintenance or service != "android.security.maintenance"
            return {"ok": True, "exitCode": 0, "stdout": "Service " + service + (": found" if found else ": not found")}
        if command == "service call android.security.maintenance 5 i32 0":
            return {"ok": True, "exitCode": 0, "stdout": f"Result: Parcel(00000000 {self.state:08x}   '........')"}
        return {"ok": True, "exitCode": 0, "stdout": ""}


class ParserTests(unittest.TestCase):
    def test_real_user_states_and_both_native_parcel_layouts(self):
        parse = parser()
        for state in (0, 1, 2):
            self.assertEqual(parse(f"Result: Parcel(00000000 {state:08x} '........')"), state)
            self.assertEqual(parse(f"Result: Parcel(\n  0x00000000: 00000000 {state:08x} '........'\n)"), state)

    def test_service_error_and_ambiguous_parcels_never_count_as_ready(self):
        parse = parser()
        for value in (None, "", "Service android.security.maintenance: found",
                      "Result: Parcel(00000000 00000004 '........')",
                      "Result: Parcel(fffffff8 00000000 '........')",
                      "Result: Parcel(00000000 00000000 00000000 '............')",
                      "Result: Parcel(0x00000004: 00000000 00000001 '........')",
                      "Result: Parcel(00000000 00000001", "x" * 5000):
            self.assertIsNone(parse(value), repr(value)[:80])


class FlowTests(unittest.TestCase):
    def test_stop_then_restart_probe_then_start_without_late_restart(self):
        probe = Probe()
        result = probe.soft_reboot(timeout=10, require_health=False, reset_ssaid=True, restart_receipt="a" * 32)
        self.assertTrue(result["ok"])
        writes = [x for x in probe.events if isinstance(x, str)]
        stop = next(i for i, x in enumerate(writes) if "ctl.stop zygote" in x)
        restart = writes.index("setprop ctl.restart keystore2")
        netd = next(i for i,x in enumerate(writes) if "setprop ctl.stop netd" in x)
        lease = writes.index("lease-quarantine")
        cellular = writes.index("/system/bin/xenoid-netctl cellular-ready")
        queried = writes.index("service call android.security.maintenance 5 i32 0")
        start = writes.index("setprop ctl.restart vendor.ril-daemon;setprop ctl.start zygote")
        self.assertLess(stop, netd)
        self.assertLess(netd, lease)
        self.assertLess(lease, cellular)
        self.assertLess(cellular, restart)
        self.assertLess(restart, queried)
        self.assertLess(queried, start)
        self.assertEqual(writes.count("setprop ctl.restart keystore2"), 1)
        self.assertFalse(any("ctl.restart keystore2" in x for x in writes[start+1:]))
        self.assertIn("settings_ssaid.xml", writes[stop])
        self.assertLess(writes[stop].index("settings_ssaid.xml"), writes[stop].index("printf '%s\\n'"))

    def test_resume_performs_no_control_or_ssaid_mutation(self):
        probe = Probe(resume=True)
        result = probe.soft_reboot(timeout=10, require_health=False, reset_ssaid=True,
                                   userspace_restart_observed=True, restart_receipt="a" * 32)
        self.assertTrue(result["ok"])
        writes = [x for x in probe.events if isinstance(x, str)]
        self.assertTrue(all(x.startswith("service ") or x == "bootstrap" for x in writes))

    def test_missing_maintenance_or_error4_leaves_zygote_stopped(self):
        for arguments in ({"maintenance": False}, {"state": 4}, {"old_pid": True}):
            probe = Probe(**arguments)
            result = probe.soft_reboot(timeout=1, require_health=False, restart_receipt="a" * 32)
            self.assertEqual(result["error"], "soft_reboot_keystore_timeout")
            self.assertTrue(probe.stopped)
            self.assertFalse(probe.started)
            self.assertNotIn("bootstrap", probe.events)

    def test_stop_acknowledgement_required_before_keystore_mutation(self):
        probe = Probe(stop_ready=False)
        result = probe.soft_reboot(timeout=1, require_health=False)
        self.assertEqual(result["error"], "soft_reboot_zygote_stop_timeout")
        self.assertNotIn("setprop ctl.restart keystore2", probe.events)
        self.assertFalse(any(isinstance(x,str) and "setprop ctl.stop netd" in x for x in probe.events))

    def test_netd_failure_keeps_framework_stopped_and_keystore_untouched(self):
        for kwargs,code in (({'netd_reset':False},'soft_reboot_netd_reset_failed'),
                            ({'control_rules':0},'soft_reboot_netd_control_unverified'),
                            ({'old_netd_pid':True},'soft_reboot_netd_control_unverified')):
            with self.subTest(kwargs=kwargs):
                probe=Probe(**kwargs)
                result=probe.soft_reboot(timeout=10,require_health=False)
                self.assertEqual(result['error'],code)
                self.assertTrue(probe.stopped)
                self.assertNotIn('setprop ctl.restart keystore2',probe.events)
                self.assertNotIn('bootstrap',probe.events)

    def test_lease_owner_failure_or_noop_never_starts_framework(self):
        for kwargs in ({'quarantine':False},{'feature_absent':True}):
            with self.subTest(kwargs=kwargs):
                probe=Probe(**kwargs)
                result=probe.soft_reboot(timeout=10,require_health=False)
                self.assertEqual(result['error'],'soft_reboot_network_quarantine_failed')
                self.assertTrue(probe.stopped)
                self.assertNotIn('/system/bin/xenoid-netctl cellular-ready',probe.events)
                self.assertNotIn('setprop ctl.restart keystore2',probe.events)

    def test_cellular_error_preserves_sanitized_native_cause(self):
        probe=Probe(cellular=False)
        result=probe.soft_reboot(timeout=10,require_health=False)
        self.assertEqual(result,{'ok':False,'code':'soft_reboot_netd_control_failed','error':'soft_reboot_netd_control_failed',
                                 'rootExitCode':1,'controlOp':'cellular-ready-addresses','controlErrno':110})
        self.assertNotIn('setprop ctl.restart keystore2',probe.events)
        self.assertNotIn('not-public',json.dumps(result))

    def test_netd_script_is_bounded_valid_shell_without_manual_routes(self):
        probe=Probe()
        self.assertTrue(probe.soft_reboot(timeout=10,require_health=False)['ok'])
        command=next(value for value in probe.events if isinstance(value,str) and 'setprop ctl.stop netd' in value)
        self.assertLessEqual(len(command.encode()),4096)
        parsed=subprocess.run(['sh','-n'],input=command,text=True,capture_output=True)
        self.assertEqual(parsed.returncode,0,parsed.stderr)
        self.assertNotIn('iptables',command)
        self.assertNotIn('ip rule',command)
        self.assertNotIn('ndc ',command)

    def test_resume_rejects_missing_receipt_and_does_not_reboot(self):
        probe = Probe(resume=True)
        probe.receipt_ok = False
        result = probe.soft_reboot(timeout=1, require_health=False,
                                   userspace_restart_observed=True, restart_receipt="a" * 32)
        self.assertEqual(result["error"], "soft_reboot_receipt_missing")
        self.assertFalse(probe.events)

    def test_bad_receipt_is_rejected_before_mutation(self):
        probe = Probe()
        result = probe.soft_reboot(timeout=1, restart_receipt="not-a-receipt")
        self.assertEqual(result["error"], "device_regeneration_state_invalid")
        self.assertFalse(probe.events)

    def test_timeout_and_identity_remain_caller_owned(self):
        self.assertIn("min(timeout, _SOFT_REBOOT_TIMEOUT_SECONDS)", CANDIDATE)
        self.assertNotIn("pm clear", CANDIDATE)
        self.assertNotIn("DeviceIdentityStore", CANDIDATE)


if __name__ == "__main__":
    unittest.main()
