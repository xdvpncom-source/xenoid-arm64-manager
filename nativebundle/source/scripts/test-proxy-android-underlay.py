#!/usr/bin/env python3
"""Runtime-free regression tests for the manifest-owned proxy underlay."""
import copy
import importlib.util
import ipaddress
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

SOURCE = Path(__file__).with_name("xenoid-proxy-engine.py")
SPEC = importlib.util.spec_from_file_location("proxy_underlay_engine", SOURCE)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)

MANIFEST = {"android": {"ipv4": "172.31.0.74", "ipv6": "fd78:656e:6f69:4::10",
                        "mac": "02:42:ac:1f:00:4a"}}
BASE = ["nsenter", "--target", "123", "--net", "ip"]


class NetworkSnapshot:
    def __init__(self, vpn=False):
        self.links = [{"ifindex": 12, "ifname": "rmnet_data0",
                       "address": MANIFEST["android"]["mac"], "addr_info": [
                           {"family": "inet", "scope": "global",
                            "local": MANIFEST["android"]["ipv4"], "prefixlen": 28}]}]
        if vpn:
            self.links.append({"ifindex": 13, "ifname": "tun0", "address": "",
                               "addr_info": [{"family": "inet", "scope": "global",
                                              "local": "10.8.0.2", "prefixlen": 24}]})
        self.calls = []
        self.raw = None
        self.missing_ipv6_address = False
        self.missing_ipv6_route = False

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:len(BASE)] != BASE:
            raise AssertionError("Unexpected namespace or command: " + repr(argv))
        args = argv[len(BASE):]
        address = MANIFEST["android"]["ipv6"]
        network = ipaddress.ip_network(address + "/64", strict=False)
        gateway = str(network.network_address + 1)
        if args in (["-j", "address", "show", "dev", "rmnet_data0", "scope", "global"],
                    ["-j", "-4", "address", "show", "dev", "rmnet_data0", "scope", "global"]):
            selected = copy.deepcopy([link for link in self.links if link.get("ifname") == "rmnet_data0"])
            # Observed host iproute2 behavior: -4 retains addr_info but omits MAC.
            if "-4" in args:
                for link in selected:
                    link.pop("address", None)
            out = self.raw if self.raw is not None else json.dumps(selected).encode()
        elif args == ["-o", "link", "show", "dev", "rmnet_data0"]:
            out = b"12: rmnet_data0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
        elif args == ["-6", "address", "show", "dev", "rmnet_data0", "to", address + "/128"]:
            out = b"" if self.missing_ipv6_address else ("inet6 " + address + "/64 scope global\n").encode()
        elif args == ["-6", "route", "show", "table", "1012"]:
            out = (str(network) + " dev rmnet_data0 proto static\n").encode()
            if not self.missing_ipv6_route:
                out += ("default via " + gateway + " dev rmnet_data0 proto static\n").encode()
        elif args in [
            ["-6", "address", "replace", address + "/64", "dev", "rmnet_data0", "nodad"],
            ["-6", "route", "replace", "table", "1012", str(network), "dev", "rmnet_data0", "proto", "static"],
            ["-6", "route", "replace", "table", "1012", "default", "via", gateway, "dev", "rmnet_data0", "proto", "static"],
        ]:
            out = b""
        else:
            raise AssertionError("Unexpected network operation: " + repr(args))
        return subprocess.CompletedProcess(argv, 0, out, b"")


class UnderlayTests(unittest.TestCase):
    def check_interface(self, snapshot):
        with patch.object(engine, "run", snapshot.run):
            return engine.android_data_interface(BASE, MANIFEST)

    def rejected(self, snapshot):
        with self.assertRaises(engine.Error) as caught:
            self.check_interface(snapshot)
        self.assertEqual(caught.exception.code, "ipv6_unsupported")

    def test_owned_underlay(self):
        self.assertEqual(self.check_interface(NetworkSnapshot()), "rmnet_data0")

    def test_active_vpn_does_not_replace_owned_underlay(self):
        snap = NetworkSnapshot(vpn=True)
        vpn_before = copy.deepcopy(snap.links[1])
        self.assertEqual(self.check_interface(snap), "rmnet_data0")
        self.assertEqual(snap.calls, [BASE + ["-j", "address", "show", "dev", "rmnet_data0", "scope", "global"]])
        self.assertEqual(snap.links[1], vpn_before)

    def test_observed_iproute_mixed_family_shape(self):
        snap = NetworkSnapshot()
        snap.links[0]["addr_info"].extend([
            {"family": "inet6", "local": MANIFEST["android"]["ipv6"],
             "prefixlen": 64, "scope": "global", "nodad": True},
            {},  # iproute2 can emit a filtered link-local entry as an empty object.
        ])
        self.assertEqual(self.check_interface(snap), "rmnet_data0")

    def test_vpn_only_is_not_an_underlay(self):
        snap = NetworkSnapshot(vpn=True)
        snap.links.pop(0)
        self.rejected(snap)

    def test_wrong_owned_ipv4(self):
        snap = NetworkSnapshot(vpn=True)
        snap.links[0]["addr_info"][0]["local"] = "192.0.2.99"
        self.rejected(snap)

    def test_missing_owned_ipv4(self):
        snap = NetworkSnapshot()
        snap.links[0]["addr_info"] = []
        self.rejected(snap)

    def test_wrong_owned_prefix(self):
        snap = NetworkSnapshot()
        snap.links[0]["addr_info"][0]["prefixlen"] = 24
        self.rejected(snap)

    def test_wrong_owned_mac(self):
        snap = NetworkSnapshot()
        snap.links[0]["address"] = "02:42:ac:1f:00:4b"
        self.rejected(snap)

    def test_missing_owned_mac(self):
        snap = NetworkSnapshot()
        del snap.links[0]["address"]
        self.rejected(snap)

    def test_wrong_returned_ifname(self):
        snap = NetworkSnapshot()
        record = copy.deepcopy(snap.links[0]); record["ifname"] = "tun0"
        snap.raw = json.dumps([record]).encode()
        self.rejected(snap)

    def test_invalid_ifindex(self):
        for value in [None, True, 0, -1, "12"]:
            with self.subTest(value=value):
                snap = NetworkSnapshot(); snap.links[0]["ifindex"] = value
                self.rejected(snap)

    def test_duplicate_owned_address(self):
        snap = NetworkSnapshot()
        snap.links[0]["addr_info"] *= 2
        self.rejected(snap)

    def test_malformed_json_and_schema(self):
        for raw in [b"{", b"{}", b"null", b"[null]", b"[{},{}]", b"\xff", b" " * 65537]:
            with self.subTest(raw=raw[:30]):
                snap = NetworkSnapshot(); snap.raw = raw
                self.rejected(snap)

    def test_bad_address_schema(self):
        for value in [None, {}, [None], ["inet"]]:
            with self.subTest(value=value):
                snap = NetworkSnapshot(); snap.links[0]["addr_info"] = value
                self.rejected(snap)

    def check_ipv6(self, snapshot, repair=False):
        with patch.object(engine, "run", snapshot.run), patch.object(engine, "tool", lambda name: name):
            engine.android_ipv6(MANIFEST, 123, repair=repair)

    def test_existing_ipv6_checks_with_active_vpn(self):
        snap = NetworkSnapshot(vpn=True)
        self.check_ipv6(snap)
        self.assertFalse(any("replace" in args for args in snap.calls))

    def test_missing_ipv6_address_still_rejected(self):
        snap = NetworkSnapshot(vpn=True); snap.missing_ipv6_address = True
        with self.assertRaises(engine.Error) as caught:
            self.check_ipv6(snap)
        self.assertEqual(caught.exception.code, "ipv6_unsupported")

    def test_missing_ipv6_route_still_rejected(self):
        snap = NetworkSnapshot(vpn=True); snap.missing_ipv6_route = True
        with self.assertRaises(engine.Error) as caught:
            self.check_ipv6(snap)
        self.assertEqual(caught.exception.code, "ipv6_unsupported")

    def test_ipv6_repair_only_touches_owned_link_and_table(self):
        snap = NetworkSnapshot(vpn=True); vpn_before = copy.deepcopy(snap.links[1])
        self.check_ipv6(snap, repair=True)
        writes = [args for args in snap.calls if "replace" in args]
        self.assertEqual(len(writes), 3)
        self.assertTrue(all("rmnet_data0" in args and "tun0" not in args for args in writes))
        self.assertEqual(snap.links[1], vpn_before)

    def test_bad_underlay_rejected_before_ipv6_repair(self):
        snap = NetworkSnapshot(vpn=True); snap.links[0]["address"] = "00:00:00:00:00:00"
        with self.assertRaises(engine.Error):
            self.check_ipv6(snap, repair=True)
        self.assertEqual(len(snap.calls), 1)


class ControlUnitReloadTests(unittest.TestCase):
    def run_install(self, previous):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source_dir = root / "source"; source_dir.mkdir()
            source = source_dir / "xenoid-proxy-agent@.service"
            source.write_bytes(b"[Service]\nExecStart=/new/agent\n")
            destination = root / "installed.service"
            if previous is not None:
                destination.write_bytes(previous)
                destination.chmod(0o644)
            for name in ["require_systemd", "install_deps", "install_prerequisites",
                         "validate_accounts", "mkdir", "install_sandbox", "atomic"]:
                stack.enter_context(patch.object(engine, name, Mock()))
            stack.enter_context(patch.object(engine, "__file__", str(source_dir / "engine.py")))
            stack.enter_context(patch.object(engine, "AGENT_SERVICE", destination))
            stack.enter_context(patch.object(engine, "systemd", lambda: True))
            stack.enter_context(patch.object(engine, "tool", lambda name: name))
            stack.enter_context(patch.object(engine, "control_paths", lambda: {}))
            # Local tests need not be root; verify that production's root-file
            # validator is invoked before an existing unit may be called equal.
            validator = stack.enter_context(patch.object(engine, "root_file", Mock()))
            install = stack.enter_context(patch.object(engine, "install_file", Mock()))
            command = stack.enter_context(patch.object(engine, "run", Mock()))
            engine.install_control({})
            installed = any(call.args[0].resolve() == source.resolve()
                            and call.args[1:] == (destination, 0o644)
                            for call in install.call_args_list)
            self.assertTrue(installed, "The unit still passes through normal installation")
            if previous is None:
                validator.assert_not_called()
            else:
                validator.assert_called_once_with(destination, 0o644)
            return command.call_args_list

    def test_identical_unit_does_not_reload_systemd(self):
        self.assertEqual(self.run_install(b"[Service]\nExecStart=/new/agent\n"), [])

    def test_changed_unit_reloads_systemd(self):
        calls = self.run_install(b"[Service]\nExecStart=/old/agent\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], ["systemctl", "daemon-reload"])

    def test_missing_unit_reloads_systemd(self):
        calls = self.run_install(None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], ["systemctl", "daemon-reload"])

    def test_unsafe_existing_unit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            target = root / "target"; target.write_bytes(b"old")
            destination = root / "installed.service"; destination.symlink_to(target)
            stack.enter_context(patch.object(engine, "AGENT_SERVICE", destination))
            for name in ["require_systemd", "install_deps", "install_prerequisites",
                         "validate_accounts", "mkdir", "install_sandbox"]:
                stack.enter_context(patch.object(engine, name, Mock()))
            install = stack.enter_context(patch.object(engine, "install_file", Mock()))
            command = stack.enter_context(patch.object(engine, "run", Mock()))
            with self.assertRaises(engine.Error) as caught:
                engine.install_control({})
            self.assertEqual(caught.exception.code, "unsafe_path")
            install.assert_not_called(); command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
