#!/usr/bin/env python3
"""Fetch only a resolved source-plan into an explicitly owned empty directory.

This script is not run by plan preparation. --execute is required to fetch.
It never installs host packages, starts Docker, builds, or touches devices.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

GIB = 1024 ** 3
HERE = Path(__file__).resolve().parent
MARKER = ".xenoid-bluetooth-build-owner.json"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--extend-plan", action="store_true",
                   help="Accept only an append-only reviewed source-plan extension")
    p.add_argument("--minimum-free-gib", type=float, default=28)
    p.add_argument("--maximum-growth-gib", type=float, default=58)
    a = p.parse_args()
    if not a.execute:
        raise SystemExit("No changes made. Reviewed execution requires --execute.")
    root = a.root.expanduser().absolute()
    if root.is_symlink() or root == Path("/") or root == Path.home():
        raise SystemExit("unsafe build root")
    plan_bytes = (HERE / "source-plan.json").read_bytes()
    plan = json.loads(plan_bytes)
    if plan["unresolved"]:
        raise SystemExit("source plan is not fully commit-pinned")
    pin = hashlib.sha256(plan_bytes).hexdigest()
    if root.exists() and root.is_dir() and not any(root.iterdir()):
        (root / MARKER).write_text(json.dumps({"plan_sha256": pin, "created": time.time()}) + "\n")
    if root.exists():
        marker = root / MARKER
        if not marker.is_file():
            raise SystemExit("refusing unowned checkout")
        owner = json.loads(marker.read_text())
        if owner.get("plan_sha256") != pin:
            old_file = root / "source-plan.json"
            if not a.extend_plan or not old_file.is_file():
                raise SystemExit("refusing nonmatching plan without --extend-plan")
            old_bytes = old_file.read_bytes()
            if hashlib.sha256(old_bytes).hexdigest() != owner.get("plan_sha256"):
                raise SystemExit("stored source plan does not match owner receipt")
            old = json.loads(old_bytes)
            new_rows = {r["path"]: r for r in plan["projects"]}
            for old_row in old["projects"]:
                new_row = new_rows.get(old_row["path"])
                if not new_row:
                    raise SystemExit("plan extension removed an existing project")
                for field in ("path", "name", "url", "commit", "links", "copies", "sparse_cone"):
                    if old_row.get(field) != new_row.get(field):
                        raise SystemExit("plan extension changed an existing project")
            owner.update(previous_plan_sha256=owner["plan_sha256"], plan_sha256=pin)
            marker.write_text(json.dumps(owner) + "\n")
    else:
        root.mkdir(parents=True, mode=0o700)
        (root / MARKER).write_text(json.dumps({"plan_sha256": pin, "created": time.time()}) + "\n")
    (root / "source-plan.json").write_bytes(plan_bytes)
    initial_free = shutil.disk_usage(root).free
    minimum_free = int(a.minimum_free_gib * GIB)
    max_growth = int(a.maximum_growth_gib * GIB)
    if initial_free < minimum_free + 6 * GIB:
        raise SystemExit("insufficient free disk for the bounded source phase")
    receipts = []

    def run(argv, *, cwd):
        with subprocess.Popen(argv, cwd=cwd) as proc:
            while proc.poll() is None:
                free = shutil.disk_usage(root).free
                if free < minimum_free or initial_free - free > max_growth:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise SystemExit("disk guard stopped checkout; owned partial tree retained")
                time.sleep(0.25)
            if proc.returncode:
                raise SystemExit("source command failed: " + repr(argv))

    for row in plan["projects"]:
        rel = Path(row["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise SystemExit("unsafe source plan path")
        dst = root / rel
        if dst.is_symlink() or not dst.resolve().is_relative_to(root.resolve()):
            raise SystemExit("project escapes the owned source root")
        if dst.exists() and not (dst / ".git").is_dir():
            raise SystemExit("non-git path already exists: " + row["path"])
        if not dst.exists():
            dst.mkdir(parents=True)
            run(["git", "init", "-q"], cwd=dst)
            run(["git", "remote", "add", "origin", row["url"]], cwd=dst)
        else:
            remote = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=dst, text=True).strip()
            if remote != row["url"]:
                raise SystemExit("source URL mismatch: " + row["path"])
            if subprocess.check_output(["git", "status", "--porcelain"], cwd=dst):
                raise SystemExit("source checkout is not clean: " + row["path"])
            prior = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=dst,
                                   text=True, capture_output=True)
            if prior.returncode == 0 and prior.stdout.strip() == row["commit"]:
                receipts.append({"path": row["path"], "commit": row["commit"]})
                continue
        run(["git", "-c", "protocol.version=2", "fetch", "--depth=1", "--no-tags",
             "--filter=blob:none", "origin", row["commit"]], cwd=dst)
        if row.get("sparse_cone"):
            run(["git", "sparse-checkout", "init", "--cone"], cwd=dst)
            run(["git", "sparse-checkout", "set"] + row["sparse_cone"], cwd=dst)
        run(["git", "checkout", "--detach", row["commit"]], cwd=dst)
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dst, text=True).strip()
        if actual != row["commit"]:
            raise SystemExit("source SHA mismatch: " + row["path"])
        receipts.append({"path": row["path"], "commit": actual})
        (root / "checkout-receipt.json").write_text(json.dumps(receipts, indent=2) + "\n")
    # Preserve the official manifest's build entrypoint links/copies, with no
    # overwrite of unknown paths and no paths outside the owned source root.
    for row in plan["projects"]:
        for entry, kind in [(x, "link") for x in row["links"]] + [(x, "copy") for x in row["copies"]]:
            src_rel = Path(row["path"]) / entry["src"]
            dst_rel = Path(entry["dest"])
            if dst_rel.is_absolute() or ".." in dst_rel.parts:
                raise SystemExit("unsafe manifest destination")
            src, dst = root / src_rel, root / dst_rel
            if not src.exists():
                raise SystemExit("missing declared source: " + str(src_rel))
            dst.parent.mkdir(parents=True, exist_ok=True)
            if kind == "link":
                expected = os.path.relpath(src, dst.parent)
                if dst.is_symlink() and os.readlink(dst) == expected:
                    continue
                if dst.exists() or dst.is_symlink():
                    raise SystemExit("manifest link destination already exists")
                dst.symlink_to(expected)
            elif not dst.exists():
                shutil.copy2(src, dst)
            elif dst.read_bytes() != src.read_bytes():
                raise SystemExit("manifest copy destination mismatch")
    (root / "source-plan.json").write_bytes(plan_bytes)
    print(json.dumps({"ok": True, "projects": len(receipts), "root": str(root),
                      "free_gib": shutil.disk_usage(root).free / GIB}))


if __name__ == "__main__":
    main()
