#!/usr/bin/env python3
"""Apply the reviewed Rootcanal source patch to an exact supported checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess

HERE = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_states(root, manifest):
    states = []
    for name, pins in manifest['files'].items():
        path = root / name
        if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('unsafe_source_file:' + name)
        digest = sha(path.read_bytes())
        choices = [state for state in ('before', 'after') if digest == pins[state + '_sha256']]
        if len(choices) != 1: raise RuntimeError('source_hash_mismatch:' + name)
        states.extend(choices)
    if len(set(states)) != 1: raise RuntimeError('partial_source_patch_requires_clean_checkout')
    return states[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bluetooth-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='otherwise validate without changing source')
    args = parser.parse_args()
    root = args.bluetooth_root.resolve()
    manifest = json.loads((HERE / 'patch-manifest.json').read_text())
    patch = HERE / 'rootcanal-cfi-ownership.patch'
    if sha(patch.read_bytes()) != manifest['patch_sha256']:
        raise RuntimeError('source_patch_digest_changed')
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if commit not in manifest['supported_commits'].values(): raise RuntimeError('unsupported_bluetooth_commit')
    modified = subprocess.check_output(['git', '-C', str(root), 'diff', '--name-only', 'HEAD'], text=True).splitlines()
    if set(modified) - set(manifest['files']): raise RuntimeError('unreviewed_bluetooth_source_changes')
    state = file_states(root, manifest)
    if state == 'before':
        subprocess.run(['git', '-C', str(root), 'apply', '--check', str(patch)], check=True)
        if args.apply:
            subprocess.run(['git', '-C', str(root), 'apply', str(patch)], check=True)
            if file_states(root, manifest) != 'after': raise RuntimeError('source_patch_readback_failed')
    print(json.dumps({'ok': True, 'commit': commit, 'already_applied': state == 'after',
                      'applied_now': bool(args.apply and state == 'before'),
                      'files': len(manifest['files']), 'cfi_disabled': False}))


if __name__ == '__main__': main()
