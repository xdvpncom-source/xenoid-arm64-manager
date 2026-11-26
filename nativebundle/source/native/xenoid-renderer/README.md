# Managed GLES renderer label

This component configures only the reported GLES `GL_RENDERER` name. The real
ANGLE/SwiftShader backend, vendor, Vulkan properties, extensions and capability
limits remain unchanged. A configured name is not proof of physical GPU hardware
or device certification. WebGL's unmasked renderer consumes this same GLES value.

`scripts/build-gles-renderer.sh` extracts the exact library from the immutable
Android 13 base using a stopped disposable Docker container. It never starts or
modifies an Android instance. The transform pins both the original library SHA
and its public ANGLE source revision. An unsupported base fails before producing
an output. `XENOID_GLES_RENDERER` defaults to `Mali-G78`; an empty value produces
the exact unmodified library with `enabled=false` in the manifest.

The ELF transform adds a read-only string segment and changes one four-byte
address instruction in `Context::getString(GL_RENDERER)`. Other original bytes,
addresses, symbols, relocations and CFI remain unchanged. The original GNU build
ID is retained; identify generated outputs by their SHA-256, not that build ID.

`scripts/build-native-renderer.sh` builds the ARM64 Android root-only mount
helper with the pinned NDK. ArtifactBuilder must register this helper and the
generated GLES payload as **live-deploy artifacts**, outside the image context.
This avoids boot-image drift and preserves existing container/user-data identity.

The native helper protocol is:

```text
xenoid-renderer status SOURCE SHA256 SOURCE_DEV SOURCE_INODE BASE_DEV BASE_INODE
xenoid-renderer bind   SOURCE SHA256 SOURCE_DEV SOURCE_INODE BASE_DEV BASE_INODE
xenoid-renderer detach SOURCE SHA256 SOURCE_DEV SOURCE_INODE BASE_DEV BASE_INODE
```

The target is fixed to `/vendor/lib64/egl/libGLESv2_angle.so`. The source is fixed
to `/data/local/tmp/xenoid-renderer/objects/SHA256/libGLESv2_angle.so`. The stage
root and its children must be root-owned private directories; the library must
be a root:root regular file, mode `0644`, one link, with the original SELinux
label. Stage the bytes and copy that metadata before requesting a bind. The
caller journals the original target's verified device/inode and SHA, stages and
checks the source, then supplies its actual device/inode. The helper independently
hashes both files and rejects an unrelated target or unsafe source.

Responses use `dev.xenoid.renderer-mount/v1`. Success includes `state` (`native`
or `bound`), `changed`, `readonly`, and actual target `device`/`inode`. Failure
includes `ok=false`, a stable `error` and `errno`. Mount operations use direct
syscalls because Android's mount text may be a profile view. A failed private or
read-only remount detaches only the newly owned bind. A partial writable bind
remains removable by its exact recorded identity. Detach uses
`MNT_DETACH|UMOUNT_NOFOLLOW`; the base filesystem is never remounted writable.

The production `up` owner must activate and verify the binding, then refresh
zygote through the official soft-reboot path with `reset_ssaid=False` whenever
old mappings remain. Verify actual zygote map device/inode afterward. A saved
receipt alone is not acceptance. GEN preserves a same-container bind; a cold
container restart requires `up` to restore it before acceptance. Disabling the
setting detaches the exact recorded bind and refreshes zygote through the same
official path. APKs, official Xenoid input and identity transactions are unchanged.

Run `python3 scripts/test-gles-renderer.py` for label validation, native SHA-256
vectors and compiled mount fault tests. Set `XENOID_GLES_ORIGINAL` to a private
pinned base-library path to additionally test the real ELF, deterministic output,
variable labels, native rollback bytes and structural corruption rejection.
