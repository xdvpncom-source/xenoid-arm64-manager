#!/usr/bin/env python3
"""Plan existing percentage deployments against fresh inventory; never submit jobs."""

import argparse
import hashlib
import json
import re
import sys


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TAG = re.compile(r"[0-9a-f]{12}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def plan(hosts, apks):
    """Use the panel's existing SHA ordering, without retaining old bindings."""
    available = []
    seen = set()
    for host in hosts:
        if type(host.get("id")) is not int or host["id"] < 1:
            raise ValueError("invalid host id")
        for device in host["devices"]:
            key = host["id"], device.get("id")
            if (type(key[1]) is not int or key[1] < 1 or key in seen
                    or not UUID.fullmatch(device.get("instance_id") or "")
                    or not TAG.fullmatch(device.get("resource_tag") or "")):
                raise ValueError("fresh inventory requires unique, verified device identities")
            seen.add(key)
            available.append((host["id"], device))
    deployments, skipped, overlaps, selected_by = [], [], [], {}
    apk_ids = set()
    # Preserve saved deployment order where independently selected APKs overlap.
    for apk in sorted(apks, key=lambda row: (row.get("job_id") or 0, row["id"])):
        apk_id = apk.get("id")
        if type(apk_id) is not int or apk_id < 1 or apk_id in apk_ids:
            raise ValueError("invalid or repeated APK id")
        apk_ids.add(apk_id)
        if apk.get("selection_mode", "percent") != "percent":
            skipped.append({"apk_id": apk_id, "reason": "explicit_device_selection_requires_review"})
            continue
        percent, digest = apk.get("percent"), apk.get("sha256")
        if type(percent) is not int or not 0 <= percent <= 100 or not SHA.fullmatch(digest or ""):
            raise ValueError("invalid APK percentage or digest")
        def ordering(row):
            host_id, device = row
            return hashlib.sha256(f"{digest}:{host_id}:{device['id']}:{device['resource_tag']}".encode()).digest()
        count = (len(available) * percent + 50) // 100
        targets = {}
        for host_id, device in sorted(available, key=ordering)[:count]:
            targets.setdefault(host_id, []).append(device["id"])
            key = host_id, device["id"]
            if key in selected_by:
                overlaps.append({"host_id": host_id, "device_id": device["id"],
                                 "previous_apk_id": selected_by[key], "final_apk_id": apk_id})
            selected_by[key] = apk_id
        deployments.append({"apk_id": apk_id, "percent": percent, "selected_count": count,
                            "total_count": len(available),
                            "targets": [{"host_id": host, "device_ids": sorted(ids)}
                                        for host, ids in sorted(targets.items())],
                            "request": {"method": "apk.deploy", "parameters": {"apk_id": apk_id, "percent": percent}}})
    return {"schema": "dev.xenoid.fleet.apk-redeployment-plan/v1", "mutates": False,
            "device_count": len(available), "deployments": deployments, "skipped": skipped,
            "overlaps": overlaps, "note": "Percentages select independently; review overlaps before submitting requests in this order."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    payload = json.load(sys.stdin)
    print(json.dumps(plan(payload["hosts"], payload["apks"]), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
