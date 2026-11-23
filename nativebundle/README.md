# Native source bundle

This bundle overlays the pinned public Xenoid commit
`c07e56b4aef8cd7e4d56eed11c84781369d29997`. `apply.py` verifies every declared
source file, accepts only stock or recorded public manager revisions, and
retains an idempotent private installation journal. It neither starts Android
nor modifies device data. `manifest.json` is independently pinned by the
installer; do not replace it with hashes discovered from an existing host.

The canonical fresh-instance template is
`examples/fingerprints/pixel-bluejay-android13.json`. It contains `REGENERATE`
sentinels, never captured identifiers. The normal `cold_profile` config field
selects it, and the normal content-addressed image builder includes its digest.
The matching Google release is the public Bluejay microG specification; each
host obtains/imports the pinned third-party payloads locally. No Google APK,
expanded Google image, signing key, device capture or private configuration is
included in this source bundle or its native artifact archive.

The source changes cover the cold profile, complete Build metadata and daemon/
RIL consumers, supported Bluejay Google policy, official regeneration networking,
owned TUN setup, proxy underlay selection, and the existing public manager fixes.
The installer uses one complete source overlay instead of applying overlapping
legacy patches again. `legacy-source-digests.json` records the eight prior
manager-modified files reconstructed from public patch history.

## Cold rendering

`ArtifactBuilder` owns `profileColdSeed`, `profileZygote`, `profileHwc`, and
`profilePacer`. The new pacer is built from the tested BufferQueue v2 source with
the normal Android NDK toolchain; it does not include the later experimental
transaction hook. The selected image context extracts its own exact base
`libgui.so` and SwiftShader library. Source-controlled transforms:

- add the global dependency `/system/lib64/libgui_pacer.so` without changing
  existing code, symbols or relocations;
- replace only the unique16-byte read-only SwiftShader config literal, retaining
  every executable byte and CFI check;
- install identical immutable regular files at `/vendor/etc/SwiftShader.ini`
  and `/vendor/sw.ini`, both with `ThreadCount=1`; no build-time shell is needed;
- set `debug.hwc.present_fps=15`, `debug.gui.frame_pacer_fps=15`, and
  `debug.hwui.fps_divisor=4` at `post-fs-data`, before zygote starts.

There are no inherited warm bind mounts. The native profile keeps1080×2400,
logical420DPI, physical429PPI and advertised60Hz. The generated
`/system/etc/xenoid/fresh-bluejay-render.json` records every installed render
asset's SHA, display contract and pacing settings. The panel reads and verifies
this contract; it does not call the older composer-changing15Hz helper.

The transformed libraries are derived from the pinned base at image build time,
not downloaded as unrelated binaries. Their exact baseline hashes are checked;
unsupported bases fail before publishing an image. Every Docker COPY payload
still passes Xenoid's complete exported-image byte verification. Property
sources use full-content SHA paths to avoid stale equal-size BuildKit inputs.

Official Xenoid touch is unchanged and reads the same logical dimensions and
native axis range. A successful source build is not a live acceptance claim:
the fresh image must pass cold boot, Build/Java191-property checks, mapped asset
hashes, actual render pacing, official touch and the real application loop.

## Bluetooth source artifact

`artifacts.json` identifies one independently hashed public release archive.
`install_artifacts.py` validates it and the original source-built-HAL loader,
then installs only its three-file Bluetooth release in
`runtime/redroid/.source-built-bluetooth`. That directory is an input to the
normal image builder. The helper never runs the binary or changes a live service.

The ARM64 Android13 service retains fatal cross-DSO CFI and LTO. The patch fixes
the Rust FFI deleter with a correctly typed C++ wrapper and shares
`ROOTCANAL_LMP` across all C++ consumers. No CFI bypass or Bluetooth-disable
path is present. See [Bluetooth build instructions](bluetooth/BUILD.md).

The controller address also has a single durable owner. The host's existing
fresh-data seed derives the initial locally administered unicast address from
native serial/AndroidID. Official GEN retains its recorded replacement target.
The daemon publishes that target through the existing root-only profile helper,
which validates the fixed path, fsyncs the file, atomically renames it, fsyncs the
directory, and checks the result. It then updates Rootcanal's supported
`vendor.bt.rootcanal_mac_address` plus its persistent counterpart before the
normal soft reboot. Updating Settings alone is insufficient: the Bluetooth
stack reads the controller and would overwrite that cached setting.

Cold init synchronously replays the existing mode0600 sidecar before framework
Bluetooth starts and after persistent properties load. It never creates or
chooses another identity. A valid prior persistent target is a recovery source;
an invalid existing sidecar fails closed. The normal cold-profile observer
checks readiness, the durable target and both properties without changing the
device. This needs the new daemon and native profile-helper artifacts; the
qualified Bluetooth HAL itself is unchanged.

## Direct I/O for the root and data images

The native pivot configures both loop devices atomically with `LOOP_CONFIGURE`
and requires the kernel's `LO_FLAGS_DIRECT_IO` readback, correct backing inode
and read-only mode before mounting. The ext4 mount options and flush/barrier
behavior are unchanged. This needs Linux5.8 or newer and a backing filesystem
supporting loop direct I/O; unsupported hosts fail before mounting instead of
silently reverting to buffered I/O.

A disposable ext4 fixture on Linux6.8 read the same32MiB file with the original
buffered path and this source path. The former populated35,115,008bytes of
backing-file cache; the latter populated0bytes. Both write tests preserved their
contents across fsync, unmount, detach and remount, with clean read-only fsck.
This removes duplicate backing-file caching; it does not promise a particular
Android working set or replace cold-boot, regeneration and workload tests.
Run `bash native/xenoid-pivot/test.sh` on Linux for the seven mocked failure
boundaries. Live acceptance must separately verify both actual loop-device
DIO flags/backing identities and the memory counters; normal image health is
not claimed to provide that extra check yet.

## Limits and tests

The profile is a coherent virtual6a runtime; the base image's camera/sensor
descriptors and native IMEI allocation remain inherited where no supported
replacement exists. A JSON template does not provide hardware-backed DRM,
hardware key custody, Play Integrity or device certification.

Runtime-free coverage includes the existing native cold-profile, metadata,
Google, TUN, proxy-underlay and soft-reboot tests, plus `tests/test_fresh_render.py`
and the pacer's normal and address/undefined-sanitized C++ tests. Platform-only
tests still require their stated Linux/root/runtime environment. Source and
artifact installation tests use temporary checkouts and never operate Android.

## Optional proxy continuity

The versioned `retry_proxy` policy uses the existing routing settings and encrypted
desired-source owner. Its report binds the same policy digest, generation and
runtime. Owned local routes can be ready while internet diagnostics are unknown
or retrying; `dataPlaneVerified` is not fabricated. Upstream failures retain the
enabled proxy, source/node/UDP/IPv6 flags and explicit domain exceptions. There is
no automatic DIRECT fallback or external readiness gate. The agent recovers a
clearly owned dead local process with configured backoff.

A manual `proxy status --check` reports real external failure without disturbing
a valid route. Its result describes that diagnostic, not continuous monitoring.
Legacy policy absence keeps strict acceptance, and unreadable state, ownership,
configuration and runtime mismatches remain guarded. These source changes require
a coordinated update-compatible daemon/host-control deployment; local unit tests
do not replace the real outage and recovery acceptance gate.

## Initial proxy boot fence

Fleet v0.1.30 supplies an optional initial proxy intent to the existing native
convergence journal before a fresh instance boots. Startup retains the existing
host DROP while the authenticated daemon imports the private source and the
owned proxy route becomes structurally ready. Reachability remains diagnostic
in `retry_proxy` mode; upstream failure does not release a DIRECT route.

The journal contains the source path and SHA, instance/data UUIDs, selected node,
all source flags and failure policy, never the source credentials. It validates
the root-owned mode0600 source again on resume. If the daemon committed the
source before a crash, authenticated export plus the exact generation, runtime
and storage binding recover that checkpoint without importing a second time. A
missing or changed source, identity or policy leaves the startup fence active.

Once the initial intent is applied, later administrator OFF remains authoritative.
An explicit disabled default uses the existing verified OFF path; generic native
instances without an initial intent keep their previous no-proxy behavior. This
is host Python orchestration only: daemon APK, compiled native artifacts and
image context inputs are unchanged. Runtime-free regression tests cover import
ordering, offline readiness, crash/resume and context identity. A fresh boot
with packet observation is still required before fleet creation.
