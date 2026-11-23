#!/usr/bin/env python3
"""Attest a pinned sparse source tree and stream/verify its Git-free payload.

No network access, checkout, build, device operation, deletion, or extraction.
Creation reads real Git objects; verification intentionally needs no .git.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import subprocess
import sys
import tarfile
import time

SCHEMA = "xenoid.source-tree-attestation/v1"
EXCLUDED_ROOT = {"source-plan.json", "checkout-receipt.json", "logs", "jobs"}


def fail(message):
    raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if not isinstance(value, str) or "\0" in value or "\\" in value:
        fail("invalid relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in {"", ".", ".."} for x in p.parts):
        fail("unsafe relative path: " + repr(value))
    if p.as_posix() != value:
        fail("noncanonical relative path: " + repr(value))
    return p


def excluded(value):
    p = relative(value)
    return (".git" in p.parts or p.parts[0] in EXCLUDED_ROOT
            or p.parts[0].startswith(("out", "dist", ".xenoid-")))


def root_path(root, rel):
    p = relative(rel)
    if excluded(rel):
        fail("private/generated path in payload: " + rel)
    # Do not follow a directory symlink while reading an archive entry.
    current = root
    for part in p.parts[:-1]:
        current = current / part
        if current.is_symlink():
            fail("symlink ancestor in payload: " + rel)
    return root / p


def git(project, args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_NO_LAZY_FETCH="1", GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(project)] + args,
                            capture_output=True, env=env, check=False)
    if result.returncode:
        fail("read-only Git operation failed: " + repr(args) + ": "
             + result.stderr.decode("utf-8", errors="replace")[:300])
    return result.stdout


def load_allow(path):
    value = json.loads(path.read_text()) if path else {}
    if not isinstance(value, dict):
        fail("symlink allowlist must be a path-to-exact-target object")
    for rel, target in value.items():
        relative(rel)
        if not isinstance(target, str) or "\0" in target:
            fail("invalid reviewed symlink target")
    return value


def check_link(rel, target, allow):
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    escaping = target.startswith("/") or resolved == ".." or resolved.startswith("../")
    if escaping and allow.get(rel) != target:
        fail("unreviewed absolute/escaping symlink: " + rel + " -> " + target)
    if rel in allow and allow[rel] != target:
        fail("reviewed symlink target changed: " + rel)


def snapshot_entry(root, rel, allow):
    path = root_path(root, rel)
    before = path.lstat()
    mode = stat.S_IMODE(before.st_mode)
    if mode & ~0o777:
        fail("special permission bits in source: " + rel)
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        target.encode("utf-8", errors="strict")
        check_link(rel, target, allow)
        data = target.encode("utf-8")
        entry = dict(path=rel, kind="symlink", mode=mode, target=target,
                     size=len(data), sha256=digest(data))
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    elif stat.S_ISREG(before.st_mode):
        sha256 = hashlib.sha256()
        sha1 = hashlib.sha1(b"blob " + str(before.st_size).encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha256.update(chunk)
                sha1.update(chunk)
        entry = dict(path=rel, kind="file", mode=mode,
                     size=before.st_size, sha256=sha256.hexdigest())
        blob = sha1.hexdigest()
    else:
        fail("unsupported source inode: " + rel)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
        fail("source changed during hashing: " + rel)
    return entry, blob


def add_directories(root, entries):
    directories = set()
    for rel in entries:
        directories.update(str(p) for p in PurePosixPath(rel).parents if str(p) != ".")
    for rel in sorted(directories):
        if rel in entries:
            fail("source directory collides with a file: " + rel)
        path = root_path(root, rel)
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode):
            fail("source ancestor is not a real directory: " + rel)
        if stat.S_IMODE(st.st_mode) & ~0o777:
            fail("special permission bits in source directory: " + rel)
        entries[rel] = dict(path=rel, kind="directory", mode=stat.S_IMODE(st.st_mode))


def cone_includes(name, cones):
    """Git cone mode includes selected trees and files in every ancestor."""
    relative(name)
    parent = posixpath.dirname(name)
    for cone in cones:
        relative(cone)
        if name == cone or name.startswith(cone + "/"):
            return True
        ancestors = {""}
        ancestors.update(str(p) for p in PurePosixPath(cone).parents if str(p) != ".")
        if parent in ancestors:
            return True
    return False


def pinned_index(project, head):
    """Compare complete real tree IDs, including intent-to-add index entries."""
    actual = {}
    for rec in git(project, ["ls-files", "--stage", "-z"]).split(b"\0"):
        if not rec:
            continue
        info, name_b = rec.split(b"\t", 1)
        mode_b, object_b, stage_b = info.split()
        if stage_b != b"0":
            fail("unmerged source index")
        name = name_b.decode("utf-8", errors="strict")
        if name in actual:
            fail("duplicate source index entry")
        actual[name] = (mode_b, object_b)
    expected = {}
    for rec in git(project, ["ls-tree", "-r", "--full-tree", "-z", head]).split(b"\0"):
        if not rec:
            continue
        info, name_b = rec.split(b"\t", 1)
        mode_b, _, object_b = info.split()
        expected[name_b.decode("utf-8", errors="strict")] = (mode_b, object_b)
    if actual != expected:
        fail("source index differs from pinned HEAD: " + str(project))
    return actual


def create(root, plan_path, output, allow_path=None, patch_path=None):
    root = root.resolve(strict=True)
    if output.resolve(strict=False).is_relative_to(root):
        fail("attestation must be written outside the source tree")
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get("unresolved"):
        fail("source plan is not fully pinned")
    allow = load_allow(allow_path)
    patch = json.loads(patch_path.read_text()) if patch_path else None
    approved = {}
    if patch:
        if patch.get("cfi_disabled") is not False or patch.get("unique_ptr_header_layout_changed") is not False:
            fail("unexpected patch contract")
        if patch.get("patch_sha256") != plan.get("source_patch_sha256"):
            fail("patch identity differs from pinned source plan")
        approved = {"packages/modules/Bluetooth/" + rel: record["after_sha256"]
                    for rel, record in patch["files"].items()}
        if len(approved) != 3:
            fail("expected the reviewed three-file Rootcanal source patch")
    entries, heads, sparse = {}, [], []
    modified_seen = set()
    for row in plan["projects"]:
        project_rel = row["path"]
        project = root_path(root, project_rel)
        if project.is_symlink() or not project.is_dir():
            fail("missing real source project: " + project_rel)
        head = git(project, ["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip()
        if head != row["commit"]:
            fail("source HEAD mismatch: " + project_rel)
        # This also proves the actual commit object exists locally.
        if git(project, ["cat-file", "-t", head]).strip() != b"commit":
            fail("source commit object missing: " + project_rel)
        index = pinned_index(project, head)
        if git(project, ["ls-files", "--others", "--exclude-standard", "-z"]):
            fail("untracked source files exist: " + project_rel)
        skip = set()
        for rec in git(project, ["ls-files", "-t", "-z"]).split(b"\0"):
            if rec and rec[:2] == b"S ":
                skip.add(rec[2:].decode("utf-8", errors="strict"))
        cones = row.get("sparse_cone", [])
        if cones:
            configured = git(project, ["sparse-checkout", "list"]).decode("utf-8").splitlines()
            if sorted(configured) != sorted(cones):
                fail("sparse-checkout selection differs from pinned plan: " + project_rel)
        for name in skip:
            if not cones or cone_includes(name, cones):
                fail("skip-worktree outside approved sparse omission: " + project_rel + "/" + name)
        omitted = 0
        for name, (mode_b, object_b) in index.items():
            rel = project_rel + "/" + name
            path = root_path(root, rel)
            if name in skip and not path.exists() and not path.is_symlink():
                omitted += 1
                continue
            if mode_b not in {b"100644", b"100755", b"120000"}:
                fail("unsupported Git source mode: " + rel)
            entry, blob = snapshot_entry(root, rel, allow)
            expected_kind = "symlink" if mode_b == b"120000" else "file"
            if entry["kind"] != expected_kind:
                fail("source inode differs from Git mode: " + rel)
            if entry["kind"] == "file" and bool(entry["mode"] & stat.S_IXUSR) != (mode_b == b"100755"):
                fail("source executable bit differs from Git: " + rel)
            if blob != object_b.decode():
                if approved.get(rel) != entry["sha256"]:
                    fail("unapproved content differs from pinned Git blob: " + rel)
                modified_seen.add(rel)
            if rel in entries and entries[rel] != entry:
                fail("project payload collision: " + rel)
            entries[rel] = entry
        heads.append(dict(path=project_rel, commit=head, url=row["url"]))
        sparse.append(dict(path=project_rel, omitted_index_files=omitted))
    if modified_seen != set(approved):
        fail("approved source patch is not applied completely")
    for row in plan["projects"]:
        for link in row.get("links", []):
            rel = link["dest"]
            source = row["path"] + "/" + link["src"]
            expected = posixpath.relpath(source, posixpath.dirname(rel) or ".")
            entry, _ = snapshot_entry(root, rel, allow)
            if entry["kind"] != "symlink" or entry["target"] != expected:
                fail("official manifest link mismatch: " + rel)
            entries[rel] = entry
        for cp in row.get("copies", []):
            entry, _ = snapshot_entry(root, cp["dest"], allow)
            src_entry, _ = snapshot_entry(root, row["path"] + "/" + cp["src"], allow)
            if entry["kind"] != "file" or entry["sha256"] != src_entry["sha256"]:
                fail("official manifest copy mismatch: " + cp["dest"])
            entries[entry["path"]] = entry
    add_directories(root, entries)
    ordered = sorted(entries.values(), key=lambda e: e["path"])
    manifest = dict(schema=SCHEMA, created_at=time.time(), source_plan_sha256=digest(plan_bytes),
                    source_heads=heads, sparse=sparse, reviewed_external_symlinks=allow,
                    approved_patch_sha256=(patch.get("patch_sha256") if patch else None),
                    approved_patch_manifest_sha256=(digest(patch_path.read_bytes()) if patch_path else None),
                    approved_modified_paths=sorted(modified_seen), entries=ordered,
                    tree_sha256=digest(canonical(ordered)))
    data = canonical(manifest)
    with output.open("xb") as stream:
        stream.write(data)
    output.chmod(0o600)
    return summary(manifest, digest(data))


def summary(manifest, manifest_sha):
    return dict(manifest_sha256=manifest_sha, tree_sha256=manifest["tree_sha256"],
                projects=len(manifest["source_heads"]), entries=len(manifest["entries"]),
                source_bytes=sum(e.get("size", 0) for e in manifest["entries"]),
                approved_modified_paths=manifest["approved_modified_paths"])


def load_manifest(path, expected):
    data = path.read_bytes()
    if digest(data) != expected:
        fail("attestation digest mismatch")
    value = json.loads(data)
    if value.get("schema") != SCHEMA:
        fail("wrong attestation schema")
    paths = [e["path"] for e in value["entries"]]
    if paths != sorted(set(paths)):
        fail("duplicate or unsorted attestation entries")
    if digest(canonical(value["entries"])) != value["tree_sha256"]:
        fail("tree digest mismatch")
    for rel in paths:
        relative(rel)
        if excluded(rel):
            fail("private/generated attestation entry")
    return value


def verify_entry(root, entry, allow):
    path = root_path(root, entry["path"])
    st = path.lstat()
    if entry["kind"] == "directory":
        if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != entry["mode"]:
            fail("source directory/mode mismatch: " + entry["path"])
    else:
        actual, _ = snapshot_entry(root, entry["path"], allow)
        if actual != entry:
            fail("source content/type/mode mismatch: " + entry["path"])


def verify(root, manifest, strict=True):
    root = root.resolve(strict=True)
    allow = manifest["reviewed_external_symlinks"]
    for entry in manifest["entries"]:
        verify_entry(root, entry, allow)
    if strict:
        known = {e["path"] for e in manifest["entries"]}
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in list(dirs):
                rel = (Path(parent) / name).relative_to(root).as_posix()
                if excluded(rel):
                    dirs.remove(name)
                    continue
                if rel not in known:
                    fail("unexpected source directory: " + rel)
                if (Path(parent) / name).is_symlink():
                    dirs.remove(name)
            for name in files:
                rel = (Path(parent) / name).relative_to(root).as_posix()
                if not excluded(rel) and rel not in known:
                    fail("unexpected source file: " + rel)
    return summary(manifest, "verified")


class CheckedReader:
    def __init__(self, path, expected):
        self.stream = path.open("rb")
        self.hash = hashlib.sha256()
        self.expected = expected
        self.count = 0

    def read(self, size):
        data = self.stream.read(size)
        self.hash.update(data)
        self.count += len(data)
        return data

    def close(self):
        self.stream.close()
        if self.count != self.expected["size"] or self.hash.hexdigest() != self.expected["sha256"]:
            fail("source changed while streaming: " + self.expected["path"])


def pack(root, manifest, output, only):
    root = root.resolve(strict=True)
    selected = set(only or [e["path"] for e in manifest["entries"]])
    all_entries = {e["path"]: e for e in manifest["entries"]}
    if not selected <= all_entries.keys():
        fail("requested delta path absent from attestation")
    for rel in list(selected):
        selected.update(str(p) for p in PurePosixPath(rel).parents if str(p) != ".")
    entries = [all_entries[p] for p in sorted(selected)]
    allow = manifest["reviewed_external_symlinks"]
    # Validate before emitting bytes as well as while reading each file.
    for entry in entries:
        verify_entry(root, entry, allow)
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry["path"])
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = entry["mode"]
            info.mtime = 0
            if entry["kind"] == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry["kind"] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry["target"]
                archive.addfile(info)
            elif entry["kind"] == "file":
                info.size = entry["size"]
                checked = CheckedReader(root_path(root, entry["path"]), entry)
                try:
                    archive.addfile(info, checked)
                finally:
                    checked.close()
            else:
                fail("unsupported archive entry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--root", type=Path, required=True)
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--output", type=Path, required=True)
    c.add_argument("--allow-symlinks", type=Path)
    c.add_argument("--approved-patch", type=Path)
    for name in ["verify", "pack"]:
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--manifest-sha256", required=True)
        if name == "pack":
            p.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    if args.command == "create":
        print(json.dumps(create(args.root, args.plan, args.output, args.allow_symlinks, args.approved_patch)))
    else:
        manifest = load_manifest(args.manifest, args.manifest_sha256)
        if args.command == "verify":
            print(json.dumps(verify(args.root, manifest)))
        else:
            pack(args.root, manifest, sys.stdout.buffer, args.only)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        print("source_tree_error: " + str(exc), file=sys.stderr)
        sys.exit(1)
