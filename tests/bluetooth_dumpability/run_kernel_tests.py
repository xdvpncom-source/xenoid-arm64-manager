"""Compile the production prctl block and exercise disposable Linux children.

No Android process or device is used. The existing host policy must already be 2;
this runner never changes sysctl. Unsupported hosts exit 77 without running C.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / 'nativebundle/source/native/xenoid-profile-runtime/xenoid_shim.c'
FIXTURE = HERE / 'test_bluetooth_dumpability.c'
SOURCE_SHA256 = '760d14679994068d888a47d5398c6b163482ddfc81906ef5b65b0d33d4143e20'
FIXTURE_SHA256 = '01c3c340fbe33f4c34629356e479e530418823bcd28da29844bdfd9362ffb75e'
POLICY = Path('/proc/sys/fs/suid_dumpable')
CASES = [
    ('com.android.bluetooth', 'target'),
    ('com.android.bluetooth', 'first-name'),
    ('com.android.bluetooth', 'uid1000'),
    ('com.android.bluetooth', 'uid10000'),
    ('com.android.bluetooth:other', 'wrong-name'),
    ('android.hardware.bluetooth-service', 'wrong-name'),
    ('zygote64-package-placeholder', 'after-name'),
    ('com.android.bluetooth', 'fork'),
    ('com.android.bluetooth', 'threads'),
    ('com.android.bluetooth', 'old-restore2'),
]


def need(value, code):
    if not value:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def block(source):
    start = source.find('/* Bluetooth dumpability initialization begins here.')
    if start >= 0:
        end = source.index('/* Bluetooth dumpability initialization ends here. */', start)
        return source[start:end]
    # Explicit alternate source/hash arguments can test the pre-fix baseline.
    start = source.index('int prctl(int option, ...)')
    at = source.index('{', start)
    depth, end = 1, at + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def checked_inputs(source=SOURCE, source_sha256=SOURCE_SHA256,
                   fixture=FIXTURE, fixture_sha256=FIXTURE_SHA256):
    raw, fixture_bytes = source.read_bytes(), fixture.read_bytes()
    need(sha(raw) == source_sha256 and sha(fixture_bytes) == fixture_sha256,
         'test_source_pin')
    return raw, fixture_bytes


def unsupported_reason():
    if sys.platform != 'linux':
        return 'Linux is required for the real syscall fixture'
    if platform.machine().lower() not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        return 'The production policy requires a supported 64-bit Linux syscall ABI'
    if os.geteuid() != 0:
        return 'UID 0 is required for disposable UID-specialization children'
    try:
        if POLICY.read_text().strip() != '2':
            return 'The existing fs.suid_dumpable policy must already be 2'
    except OSError:
        return 'The existing fs.suid_dumpable policy is unavailable'
    if shutil.which('cc') is None:
        return 'A host C compiler named cc is required'
    return None


def run_tests(output, *, source=SOURCE, source_sha256=SOURCE_SHA256,
              fixture=FIXTURE, fixture_sha256=FIXTURE_SHA256):
    raw, fixture_bytes = checked_inputs(source, source_sha256, fixture, fixture_sha256)
    need(unsupported_reason() is None, 'unsupported_kernel_test_host')
    output.mkdir(mode=0o700, exist_ok=False)
    extracted = block(raw.decode())
    (output / 'prctl-policy-under-test.inc').write_text(extracted)
    (output / 'test.c').write_bytes(fixture_bytes)
    argv = ['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
            str(output / 'test.c'), '-o', str(output / 'test')]
    start = time.time()
    built = subprocess.run(argv, capture_output=True, timeout=45)
    (output / 'build.stdout').write_bytes(built.stdout)
    (output / 'build.stderr').write_bytes(built.stderr)
    need(built.returncode == 0, 'C_fixture_compile_failed')
    results = []
    for name, case in CASES:
        before = POLICY.read_text()
        need(before.strip() == '2', 'host_policy_changed_before_case')
        child = subprocess.run([name, case], executable=str(output / 'test'),
                               capture_output=True, timeout=15)
        need(POLICY.read_text() == before, 'host_policy_changed')
        results.append({'case': case, 'argv0': name, 'returncode': child.returncode,
                        'stdout': child.stdout.decode(), 'stderr': child.stderr.decode()})
    result = {
        'schema': 'xenoid.bluetooth-dumpability-real-kernel-tests/v1',
        'ok': all(row['returncode'] == 0 for row in results),
        'started': start, 'completed': time.time(),
        'source_sha256': sha(raw), 'fixture_sha256': sha(fixture_bytes),
        'exact_compiled_block_sha256': sha(extracted.encode()),
        'compiler_argv': argv, 'compiler_returncode': built.returncode,
        'host_suid_dumpable_before_after': '2', 'Android_actions': 0,
        'sysctl_mutations': 0, 'cases': results,
    }
    destination = output / 'result.json'
    destination.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    destination.chmod(0o600)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--source-sha256', default=SOURCE_SHA256)
    parser.add_argument('--fixture', type=Path, default=FIXTURE)
    parser.add_argument('--fixture-sha256', default=FIXTURE_SHA256)
    parser.add_argument('--output', type=Path, required=True,
                        help='New private output directory; must not exist')
    args = parser.parse_args()
    # Verify public inputs even when the kernel portion will be skipped.
    checked_inputs(args.source, args.source_sha256, args.fixture, args.fixture_sha256)
    reason = unsupported_reason()
    if reason:
        print(json.dumps({'ok': False, 'skipped': True, 'reason': reason,
                          'sysctl_mutations': 0, 'Android_actions': 0}))
        raise SystemExit(77)
    result = run_tests(args.output, source=args.source, source_sha256=args.source_sha256,
                       fixture=args.fixture, fixture_sha256=args.fixture_sha256)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
