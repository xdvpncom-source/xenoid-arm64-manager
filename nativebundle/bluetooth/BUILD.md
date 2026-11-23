# Reproduce the Android13 Bluetooth service

The public release supplies the verified ARM64 service as a separate asset so
ARM64 device hosts do not need a second full Android build toolchain. Its
corresponding source is identified by the319 exact repository commits in
`build/source-plan.json`, the three-file `rootcanal-cfi-ownership.patch`, and
`patch-manifest.json`. All are public sources. The patch's SHA256 is
`7c2ed6b3fdcfbebcca47bd5b5d9c31e2c3506a9716c32cf8c5348295b5398cac`.

Use a separate Linux x86_64 builder with at least12GiB available build memory
and sufficient source/output disk. These are compiler requirements, not the
Android device's1CPU/2GiB allocation. The pinned Android13 prebuilts are
Clang `r450784d`14.0.6 and Rust1.59.0. Do not substitute an NDK-only HIDL build,
disable CFI, remove missing source modules or use placeholder libraries.

1. Fetch the explicit plan into a new, owned source root with
   `build/checkout_selected.py --root /work/source --execute`. Its disk bounds
   retain an interrupted owned checkout and never remove an existing tree.
   This plan is the final source closure, including actual Skia sources and
   the required API snapshots/type providers.
2. Run `apply_source_patch.py --bluetooth-root
   /work/source/packages/modules/Bluetooth --apply`. It checks the exact
   upstream commit and all three before/after hashes. The r74 Bluetooth commit
   is `afe918573bddb9bf4c657fd86a2fdaeb4526f9d7`.
3. Generate the normal Kati-enabled Soong graph using the pinned product
   `redroid_arm64-userdebug`. `build/build_selected.sh graph /work/source`
   records that invocation and runs the initial bootstrap Go tests. Its build
   phase skips only repeating those already-passed tests. Do not use
   `--soong-only`: this release's graph emits duplicate unrelated ICU install
   rules in that mode. Keep Ninja `dupbuild=err`.
4. A sparse build may stop later in unrelated Kati packaging after writing a
   complete Soong graph. Inspect that actual failure; do not claim it passed.
   Verify the unchanged `combined.ninja` contains only the generated Soong
   subninja with its ordinary pools, and that the selected ARM64 service's
   strict dry-run closure has no missing dependency/error rule.
5. Build the real host `aprotoc`, `hidl-gen`, `ndkstubgen` and host `libc++`
   targets, then reproduce only their exact generated Kati install rules.
   `build/build_host_prerequisites.sh` performs that bounded, checked operation
   in the documented `/work/source` layout. It does not fabricate build edges.
6. `build/build_selected.sh build /work/source` invokes the official UI with
   the exact stripped and unstripped ARM64 vendor CFI service targets. The
   unused32-bit and passthrough `impl-sim` outputs are unnecessary.
7. Capture the actual selected `ninja -t commands` output. Run
   `capture_target_commands.py` to expand retained response files. Compile
   databases can select the wrong host/vendor variants and are insufficient.
8. `build/source_tree.py create`/`verify` bind the complete checked source
   tree, modes, links and approved patch. `validate_hal.py` consumes that
   manifest, both ELF outputs, actual commands and the pinned base image's
   dependency-symbol/namespace evidence. Require every check to pass: AArch64,
   exact dependencies/versioned imports, preserved CFI and LTO, common
   `ROOTCANAL_LMP`, identical loaded stripped/unstripped segments, and a typed
   deleter with a direct call to `link_manager_destroy`.

`qualification.json` records the public static result used for this release.
The service SHA256 is
`2eb818005f54c77d7456d3c0910485c7bc0f1b5dd56c5e2c8c21771a32c55e29`
(3,455,000 bytes), ELF BuildID `3c9b9a022af5d5fd4a033555d1decdc8`.
The source-attestation SHA is
`bafcc5824015c5a08e1a2b94a4ec4bd0e08fa032303ef9d3af8ff34c6dd7104c`.

The release archive retains notices/licenses from the complete pinned build
source closure, an intentionally overinclusive inventory. It also contains a
static ABI reference without device IDs, endpoints, timestamps or live process
information. A new source build must produce its own fresh full qualification;
never edit a failed report or relabel a different binary as this artifact.

Static success does not establish runtime health. Fresh image acceptance must
check actual mapped service/dependency hashes, Bluetooth OFF/ON/reset cycles,
new crash/tombstone events and normal application behavior. Use the manager-only
protobuf state view; the inherited Android13 full AdapterService diagnostic dump
can abort during stack startup and is not a health probe.
