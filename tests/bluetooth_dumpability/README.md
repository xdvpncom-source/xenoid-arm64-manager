# Bluetooth dumpability regression

This fixture compiles the complete marked policy and `prctl` entry point from
the bundled production `xenoid_shim.c`, with exact source and fixture SHA256
pins. The C fixture is the unchanged original ten-case kernel regression.
Only the pre-existing process-name filter dependencies are inert; UID changes,
fork, threads, `/proc`, signal masks and `prctl` use real Linux syscalls.

From the repository root, the portable checks are part of normal discovery:

```sh
python3 -B -m unittest discover -s tests/bluetooth_dumpability -v
```

The real-kernel case skips unless the test process is UID 0 on supported 64-bit
Linux, `cc` is installed, and the **existing** `fs.suid_dumpable` value is 2.
Neither the runner nor fixture changes that host policy. A skip is not kernel
qualification. Ordinary CI still checks source/fixture pins and drift rejection.

On an authorized suitable Linux host, retain the full build and case evidence:

```sh
sudo python3 -B tests/bluetooth_dumpability/run_kernel_tests.py \
  --output /root/xenoid-bt-policy-test-unique
```

Choose a new output directory. It is created with mode `0700`; `result.json` uses `0600`.
The standalone runner exits 0 only when all ten cases pass, 1 on a case failure,
and 77 for an unsupported host. Compile/input failures remain errors. It uses
paths relative to this checkout and never downloads a compiler or dependency.

The cases cover target initialization, first-call process naming, unrelated
UIDs and package names, name specialization after fork, forked-PID cache
isolation, concurrent first calls, preserved errno/signal masks, real debuggerd
SET1/restore0 behavior and the pre-fix kernel SET2 failure. The old SET2 case
intentionally proves that Linux rejects restoring 2 with EINVAL; no result is
fabricated. Every test child is disposable and has a 12-second alarm.

For an explicit pre-fix comparison, `--source PATH --source-sha256 SHA256`
accepts a caller-pinned alternate source. The default always requires the
bundled candidate bytes. Any fixture override also needs its matching explicit
`--fixture-sha256`.

These tests start no Android process, inspect no device, modify no APK/image,
and contain no host identities, endpoints or credentials. They qualify the
isolated kernel policy; Android interposition and cold/GEN behavior require
separate real runtime qualification.
