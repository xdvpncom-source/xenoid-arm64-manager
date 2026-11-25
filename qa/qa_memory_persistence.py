"""Keep an existing QA memory policy through an installer daemon-reload.

Only memory.high is synchronized. No guest command, profile replay, proxy
recovery, lifecycle operation, worker stop, or hard resource limit is changed.
"""
from __future__ import annotations

import argparse
import json
import os
import re

import qa_resource_runtime as resources


def persist_one(name, config, *, verify=False):
    if not re.fullmatch(r"phone-[1-9][0-9]*", name):
        raise ValueError("Invalid native instance name")
    manager = resources.native_manager(name)
    owned, _ = manager._owned_container_record(timeout=8)
    if owned is None:
        # This native API collapses inspect outages and foreign ownership into
        # the same None result as absence. None is never proof of a safe skip.
        raise RuntimeError("memory_persistence_owned_runtime_unproven")
    running = (owned.get("State") or {}).get("Running")
    if type(running) is not bool:
        raise RuntimeError("memory_persistence_runtime_state_unproven")
    if running is False:
        return {"instance": name, "ok": True, "skipped": "stopped"}
    # Wait for a normal device operation, with no attempt to terminate it.
    # Refresh ownership and policy after acquiring the same native owner.
    with manager.instance_operation_lock(timeout_seconds=60):
        owned, _ = manager._owned_container_record(timeout=8)
        if owned is None:
            raise RuntimeError("memory_persistence_owned_runtime_unproven")
        running = (owned.get("State") or {}).get("Running")
        if type(running) is not bool:
            raise RuntimeError("memory_persistence_runtime_state_unproven")
        if running is False:
            return {"instance": name, "ok": True, "skipped": "runtime_stopped_while_waiting"}
        config = resources.load_config()
        if not config["enabled"] or config["render_fps"] is None:
            return {"instance": name, "ok": True, "skipped": "memory_policy_disabled"}
        for journal in ("device-regenerate-v3.json", "convergence-v1.json"):
            if os.path.lexists(manager.context.state_root / journal):
                raise RuntimeError("memory_persistence_native_transaction_pending")
        result = resources.ensure_memory_headroom(
            manager, owned["Id"], reserve_mib=config["memory_reserve_mib"],
            operation="inspect" if verify else "apply")
        if result.get("ok") is not True or result.get("hardCapsChanged") is not False:
            raise RuntimeError("memory_persistence_readback_unproven")
        if (config["memory_reserve_mib"] == 0
                and result.get("skipped") == "disabled_no_owned_receipt"
                and result.get("changed") is False and result.get("cgroupWrites") == 0):
            # The policy owns nothing to persist or restore. Preserve this
            # explicit native result, including unrelated administrator caps.
            return {"instance": name, "ok": True, "skipped": "disabled_no_owned_receipt",
                    "unit_or_kernel_policy_verified": False, "hardCapsChanged": False}
        wanted = ("max" if config["memory_reserve_mib"] == 0 else
                  str(resources.MAX_BYTES - config["memory_reserve_mib"] * 1024 * 1024))
        systemd = result.get("systemd") or {}
        if (result.get("afterHigh") != wanted or systemd.get("backend") != "systemd"
                or systemd.get("memoryHigh") != ("infinity" if wanted == "max" else wanted)):
            raise RuntimeError("memory_persistence_policy_not_satisfied")
        # inspect deliberately preserves all owner journals. It also checks
        # both systemd and kernel values, rather than treating raw high as ACK.
        return {"instance": name, "ok": True, "operation": "verify" if verify else "apply",
                "changed": result.get("changed"), "afterHigh": result.get("afterHigh"),
                "systemd": result.get("systemd"), "hardCapsChanged": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.verify:
        print(json.dumps({"inert": True, "mutations": 0}))
        return 0
    if os.geteuid() != 0:
        parser.error("Run memory persistence management as root")
    config = resources.load_config()
    if not config["enabled"] or config["render_fps"] is None:
        print(json.dumps({"ok": True, "skipped": "memory_policy_disabled"}))
        return 0
    failed = False
    for name in resources.registry_names():
        try:
            result = persist_one(name, config, verify=args.verify)
        except Exception as error:
            result = {"instance": name, "ok": False, "code": "memory_persistence_unproven",
                      "reason": resources.safe_failure_reason(error)}
        failed |= result.get("ok") is not True
        print(json.dumps(result, sort_keys=True), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
