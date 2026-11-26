# Xenoid ARM64 Fleet

The v0.1.52 release adds configurable Android locale, time-zone, date/clock format, font size and theme selection after official regeneration, with per-device baseline restoration. The native helper applies actual Android settings, and the shared script controls the choices. See the [release notes](docs/releases/v0.1.52.md). Official Xenoid native input remains the input path.

Install Xenoid on an ARM64 Ubuntu server and manage headless Android 13 instances through a local CLI. Each numeric ID maps to an upstream instance name. For example, `2` selects `phone-2`. Xenoid allocates the container, persistent volume, network, and loopback ADB port for each instance.

The [v0.1.39 release notes](docs/releases/v0.1.39.md) describe native identity
reservations for panel-managed regeneration and profile-selected CPU hardware
labels. The earlier panel-controlled native lock waits, fresh official Back
verification and memory headroom remain.
The earlier [bounded profile observation](docs/releases/v0.1.35.md) and
[regeneration recovery](docs/releases/v0.1.34.md) changes are included.
Long-running fleet qualification is recorded separately.

## Install

Use an **ARM64 (aarch64) server running Ubuntu 24.04**, such as a Hetzner CAX server. The installer requires root access, outbound HTTPS, and free disk space for the build toolchain, container images, and Android data. Install and management commands use the same operator account and Xenoid registry.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.52/install.sh | sudo -H bash -s -- --accept-android-licenses
```

The `--accept-android-licenses` flag accepts the Android SDK licenses for the pinned build tools from Google. The installer prepares the host and creates fresh Pixel 6a devices up to the host’s reserved capacity. Allow tens of minutes for the first build on a small server. The CLI is installed as `/usr/local/bin/xenoid-fleet` and the managed checkout lives under `/opt/xenoid-fleet/upstream`. Rerunning the installer reuses the toolchain, creates missing IDs and converges existing matching Pixel 6a instances. An incompatible existing profile is rejected; deletion is a separate explicit operation.

Android runs natively on ARM64 under Docker. An existing Docker engine needs Buildx and the containerd image store, which is the default on new Docker Engine 29 installations. The installer enables x86-64 userspace emulation for Google's Linux SDK and NDK executables. The Xenoid build cache and Docker images stay on the server for subsequent devices. The fleet CLI gives image builds separate Buildx client state so Xenoid can verify their payloads against the current build inputs.

The installer loads the IPv4 and IPv6 kernel netfilter tables that Xenoid checks during startup. It verifies and applies patches to the pinned Xenoid source. The standalone daemon build supplies the same package and version metadata as Gradle and reads Java source as UTF-8. The KeyMint build remaps paths from the installed Rust toolchain before artifact validation. The shared runtime inventory inspects containers individually so large fleets fit within the command output limit while retaining the same ownership checks.

Check that Android booted. The command output includes the value `1` after boot completes.

```bash
sudo -H xenoid-fleet run 1 -- adb shell getprop sys.boot_completed
```

Use `--install-only` to prepare the host before creating the first device or upgrade an existing installation without issuing a device start. Upgrades also refresh enabled proxies on already-running devices when their native control/process revision has not been verified.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.52/install.sh | sudo -H bash -s -- --install-only --accept-android-licenses
```

## Devices

| Action | Command |
| --- | --- |
| List devices and runtime status | `sudo -H xenoid-fleet list` |
| List devices as JSON | `sudo -H xenoid-fleet list --json` |
| Read automatic capacity | `sudo -H xenoid-fleet capacity --json` |
| Create and start device 2 | `sudo -H xenoid-fleet create 2` |
| Create or recreate device 2 with a profile | `sudo -H xenoid-fleet create 2 ./qa-profile.json` |
| Apply one profile to devices 2 and 3 during create or recreate | `sudo -H xenoid-fleet create 2,3 ./qa-profile.json` |
| Show the resources that deletion would release | `sudo -H xenoid-fleet delete 2 --dry-run` |
| Delete devices 2 and 3 and their Android data | `sudo -H xenoid-fleet delete 2,3 --yes` |

Use a positive integer such as `2` as the device ID. Creating a registered device recreates its container and keeps its Android data. Xenoid's `up` completes startup and verifies readiness before `create` applies the optional profile. `delete` permanently removes the selected devices' owned volumes and Android data. Use `--dry-run` to verify the targets first. Both `delete` and `run` accept `all` for the numeric devices in the managed checkout.

The optional profile uses Xenoid's `dev.xenoid.fingerprint/v1` JSON format. The CLI validates the file before boot and applies it through upstream once the device is ready. Each device keeps its own instance identity. Use `--no-google-services` when creating a new instance that should omit the configured Google-services provider.

### Optional Chrome baseline

Place an independently signature-verified ARM64 Google Chrome APK or split bundle privately at `/opt/xenoid-fleet/chrome-baseline`. Include the matching Trichrome Library when the package requires it. Use owner and group `root:root`, mode `0700` for the directory and `0600` for every file referenced by `manifest.json` and the manifest itself. Keep the APKs and their manifest private. The Fleet release supplies the loader.

`manifest.json` uses schema `dev.xenoid.fleet.chrome-baseline/v1`, a numeric Chrome `version_code`, and a `chrome` array of APK records with `base.apk` first. Set `trichrome` to the APK record for `trichrome.apk` when required or to `null` for a Chrome package that runs independently. Each APK record has a leaf `file` name and its lowercase `sha256` digest.

For Chrome builds that require size seals on Android shared memory, use schema `dev.xenoid.fleet.chrome-baseline/v2` and add a `runtime` record of the form `{"file":"libcutils.so","sha256":"<digest>"}`. Supply the matching private ARM64 Android 13 `libcutils.so` with the [AOSP memfd size-seal fix](https://android.googlesource.com/platform/system/core/+/46d46dc46446f14f26fbe8fb102dd36c1dfc1229%5E%21/). Fleet creates a separate, content-addressed image based on the pinned Redroid image and verifies the running image and Android library before installing Chrome. A plain `xenoid-fleet run ID -- up` applies this baseline to instances already configured with that image. Explicit `create` selects the compatibility image for new devices and migrates devices based on the pinned Redroid image during recreation.

`xenoid-fleet create` verifies the bundle before creating or recreating a device. After startup it installs standalone Chrome and Trichrome APKs through Xenoid `app install` and installs Chrome split APKs in one instance-scoped `adb install-multiple -r` transaction. It then checks the installed Chrome version. Existing equal or higher Chrome versions are preserved. The v1 baseline also applies during plain `up`. `--install-only` limits the installer to host preparation.

### Optional proxy default

Create the root-owned directory `/opt/xenoid-fleet/proxy-default` with mode `0700`. Use the native `proxy export --out` command on a configured instance to create `source.conf`. Keep that file and `manifest.json` root-owned with mode `0600`. The manifest contains `schema` set to `dev.xenoid.fleet.proxy-default/v1`, the exported `kind`, the hex `sha256` of `source.conf`, and the Boolean policy fields `udpAllowed`, `ipv6Allowed`, and `allowInsecureHttp`. Optional `selectedNode` and `enabled` fields default to an empty selection and `true`; older manifests remain valid.

On fresh creation with this default, Fleet binds a credential-free initial-proxy intent to the new instance and data UUID before native `up`. The existing startup DROP remains until the authenticated source import and native routing proof complete. The journal records the source file SHA and all flags, selected node and retry policy, so an interrupted import resumes from the same intent; a missing or changed file keeps the boot fence in place. A completed initial intent does not override a later administrator OFF action. Explicit `enabled=false` permits the normal verified OFF path; an absent default preserves the generic no-proxy behavior. Existing configured devices keep their normal reconciliation path. Fresh defaults select the versioned `retry_proxy` policy: startup requires a valid owned transparent route, while internet reachability is an optional diagnostic. Existing saved policies without that opt-in retain legacy strict data-plane checks. Create, recreate, and plain `up` keep the configured source, node and UDP/IPv6 flags through native reconciliation.

Native acceptance reads current host ownership, process, rules, generation and runtime epoch. A continuity report must also match the daemon-owned encrypted failure policy digest. Structural routing readiness does not claim internet availability; only an actual explicit diagnostic can set `dataPlaneVerified=true`.

The patch also preserves the rules and traffic counters already verified during
candidate staging when committing that candidate. Commit checks the pending
process, health, current structure and runtime identity before promoting its
metadata. Acceptance reads fresh host counters after the daemon report and
continues to require matching generation, runtime epoch and counter proof.
Legacy mode and explicit internet diagnostics additionally require the requested
capability matrix. Stale snapshots cannot accept an unowned or reset route.
Startup planning uses that same combined daemon/host proof, allowing a healthy
enabled proxy to converge instead of remaining permanently pending on the
host-only disabled-state flag.

The installer refreshes each running, configured, enabled proxy through native
prerequisite, quarantine, agent stop and desired-state reconciliation. It checks
the original container, instance identity, data mount and complete source/policy
fingerprint, then requires the policy-selected native route proof. It preserves the selected
node and never starts stopped devices or enables disabled/unconfigured proxies.
Pending native identity transactions are left for their own resume path.

Successful refresh markers bind the engine source/control digest and native
server process identity to the immutable device. Later installer runs require
that marker, current installed files and fresh proof before skipping. The native
server record does not attest its loaded script digest, so a missing marker
requires one verified rebootstrap even if files on disk are already current.
Failures remain explicit and keep the original runtime quarantined when its
binding is still valid. Retry selected devices with:

```bash
sudo -H xenoid-fleet qa upgrade-proxy 1,2 --timeout 180
```

`all` selects all registered numeric instances; `--timeout 0` waits without a
lock/setup deadline. The same refresh runs for `--install-only` upgrades.

### Root helper recovery

If an interrupted provisioning step leaves the verified current root helper
running without its process record, native setup can recover it through the
current authenticated protocol. Recovery requires the exact published binary,
root process owner, command line, loopback listener, instance paths and process
start identity, then revalidates ownership before the official stop and normal
helper setup. Unknown processes, unreadable ownership evidence and failed authentication remain
rejected; a failed lookup is never treated as proof that a helper is absent.
This host-side recovery keeps Android images and device identities in place.
If Android's daemon is unavailable during a framework restart, a current helper
with a valid owned process record can authenticate directly through its official
root protocol. Container and process identity are checked around that probe;
unauthorized responses are rejected. An identical published helper file already
on the device can also use normal startup without a redundant ADB upload, after
its ownership, mode, link count, size and digest are verified and rechecked before
launch. Missing or different files retain the normal deployment path.

Listener collection filters each guest TCP table to its header and LISTEN rows
for the exact leased local port before the existing bounded capture. Large
unrelated connection tables therefore do not hide a valid root-helper listener.
Both-family read/filter failures, ambiguous listeners and non-loopback addresses
still fail ownership checks.

### Fresh Pixel 6a defaults

New devices select the native cold profile before their first Android boot. The
profile reports Pixel 6a, a coherent fixed Android 13 Build tuple, 1080×2400 at
420 DPI and 429 physical PPI, with a 60 Hz display mode. Source-owned compositor,
GUI and HWUI pacing limits rendering to 15 FPS. The APK and official Xenoid touch
path are unchanged. Native GEN owns unique identity values; fixed Build and
screen metadata are not randomized. Camera and sensor capabilities remain those
supported by the underlying Android image; this profile is not physical-device
or Google-certification equivalence.

Docker enforces one CPU, 2 GiB RAM and at most 512 MiB swap per device. The
profile declares 6 GiB of model memory. Some framework memory APIs still return
host RAM; profile values do not establish agreement across every API. Read the
recommended count without changing any runtime:

```bash
sudo -H xenoid-fleet capacity --json
```

Automatic sizing reserves one host CPU and 1 GiB of real host RAM, then uses the
smaller number of remaining CPU slots or complete 2-GiB physical-RAM slots.
Swap does not increase this count; its separate check requires 512 MiB per device.
The measured 4-CPU/7.548-GiB hosts therefore fit three devices, and the
8-CPU/15.212-GiB host fits seven. The read-only capacity command accepts
`--memory-reserve-mib` and `--cpu-reserve` for larger explicit host budgets;
their minimums remain 1024 MiB and one CPU. A numeric device count remains an
explicit administrator override and reports overcommit. The panel accepts
`auto` for new hosts and device-count operations.
`--install-only` installs source, tools and defaults without creating a device;
normal installation uses the automatic count. `create ID --if-missing` avoids
forced recreation while still converging an existing matching instance.

This release does not convert old Android data to a different Google/product
binding. For an authorized fresh migration, stop scripts, explicitly delete the
old owned instances, install the release, then create the new devices. Preserve
controller APK uploads, assignments, SSH inventory, proxy sources and domain
exceptions. Archived old profile/resource settings can be restored separately;
ordinary installation does not erase those settings.

The installer validates one public native source bundle against the pinned
upstream commit. Source-built Bluetooth HAL provenance is supplied as a separate
hash-verified release asset. Pacer, profile renderer and framework metadata
artifacts are built through the existing native ArtifactBuilder and included in
the content-addressed cold image. No private Google APKs, identifiers, credentials
or runtime captures are included in the source or release asset.

Native touches now require the official driver's kernel axes and DIRECT property
to match Android's InputReader and InputDispatcher mapping. A healthy driver
that Android missed while its event node was being published is reannounced
using the same verified character-device inode. No touch event, alternate input
backend, driver replacement or image rebuild is used for this repair. The SDK
checks this before each official tap and before confirming or replaying a
completed regeneration receipt.

`Device.input_status(timeout=None)` reads readiness without repairing or sending
input. It returns the `xenoid_native` backend, `ready`, mapping and foreground
context, and `events_injected: false`. A missing timeout uses the panel command
timeout; `0` waits without a deadline. Input readiness does not establish an
app's VPN connection or the health of its remote servers. The host dependency
`qa_input_ready.py` is installed and integrity-pinned alongside the SDK.

Foreground inspection reads the current display state, excluding archived
`LAST ANR` snapshots from Android's default WindowManager dump. It accepts one
unambiguous focused window on the selected display (display 0 by default)
and retains the before/after focus checks around UI inspection and official native input. System dialogs therefore
remain visible to the script after an earlier ANR.

`Device.inspect_window(windows=True)` reads Android's stock all-window tree,
including real close buttons exposed outside the main WebView and separate
SystemUI navigation controls. The v18 scenario uses these semantic controls;
it does not guess an X coordinate. Its bounded store-return recovery applies
only to a recently observed ad-close redirect and uses a freshly observed
SystemUI Back button through `Device.tap`. The SDK's native touch route is
unchanged. A successful close still requires a fresh unobstructed app view.

`qa_ui_reader.py` keeps one private stock-UIAutomator JVM per script iteration,
so repeated observations do not repeatedly start ART and compile the command.
Every request still invokes Android's stock DumpCommand. The helper accepts
requests only over its private ADB pipe and implements no input actions or
network listener. The broker closes it at iteration completion and before
regeneration. PID, process start time, namespace, UID and private-path checks
fence cleanup to that helper and its compiler children. Existing positional
`inspect_window` calls remain supported; `reader="legacy"` explicitly selects
the old one-shot reader for diagnosis. A failed persistent read is reported,
without silently substituting another reader or input path.

For a confirmed full-screen AdActivity with no individually recognized Close, the v17 scenario can use a freshly observed SystemUI Back after the editable 15-second grace. It permits one attempt per semantic ad state and still requires a fresh unobstructed app view. MainActivity, terms, permission and VPN windows do not use this path.

The v18 scenario closes any surviving app process before launch (`COLD_START_APP=True`), preserving app data until the scheduled end-of-cycle clear. This lets a stopped or replaced run start from a fresh app process instead of a surviving Connecting/Disconnect screen. Set the script option to False to preserve the existing process. Ad retry identity uses semantic control/content fields; transient accessibility window IDs are diagnostic metadata only.

The v18 scenario's idle monitor checks focus every five seconds and takes a
full fresh UI observation when focus changes or at most ten seconds after the
last full observation. Timings are editable in the script. Controls and touch
coordinates are never reused from a cached observation after an action.

The fresh default configuration is `/etc/xenoid-fleet/qa-resources.json`:

```json
{"enabled":true,"fps":60,"render_fps":15,"scale":null,"min_side":1080,"dpi":420,"timeout":0}
```

`fps` describes the native display mode; `render_fps` verifies the separate
source-owned pacing contract. The fresh profile keeps 1080×2400/420 and derives
official input coordinates from that same geometry. `timeout: 0` permits an
unlimited setup wait. The runtime verifies the actual cold-image assets, live
properties, display, official touch mapping and cgroup limits before accepting
this profile. It does not turn an incompatible old runtime into a fresh one.

The panel supplies each device's APK assignment and saved script version.
Installing the addon does not select an app, clear app data or begin a QA cycle
by itself. Workflow revisions are independent of Fleet installer releases:
the panel's saved version governs execution on existing and newly assigned
devices, so changing ad selectors or timing does not require a Fleet upgrade.

`qa/vpn-cycle.py` is the release-time reference protected by the installer
manifest. The current panel workflow is also mirrored in
[`examples/current-vpn-cycle.py`](examples/current-vpn-cycle.py) and exported as
the [`vpn-cycle-v19.py` release asset](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.31/vpn-cycle-v19.py)
with a [SHA-256 sidecar](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.31/vpn-cycle-v19.py.sha256).
The historical v0.1.17 `vpn-cycle-v14.py`, v0.1.16 `vpn-cycle-v14.py`, v0.1.15 `vpn-cycle-v13.py`, v0.1.14 `vpn-cycle-v13.py`, v0.1.13 `vpn-cycle-v12.py` and v0.1.12 `vpn-cycle-v11.py` assets remain unchanged. Updating the
editable workflow never rewrites an existing release tag or its installer pins.

Connect readiness uses one `CONNECT_READY_SECONDS` budget, including a supplied
initial snapshot and later UI observations. Missing or ambiguous controls are
observed again through the existing dialog/Home/loading preparation; `0` waits
without a deadline. An explicit `CONNECT_POSITION` retains its configured
behavior. No automatic screen-center fallback is added.

```bash
printf '%s\n' '{"enabled":true,"fps":30,"scale":"minimum","min_side":240,"dpi":96,"timeout":180}' |
  sudo -H xenoid-fleet qa configure --json-stdin
sudo -H xenoid-fleet qa reconcile all --force
```

Create/recreate/start/up apply the profile after native startup and fingerprint
profile replay. `xenoid-qa-resources.timer` also handles running containers after
a new container boot or configuration revision. It records container ID,
Docker `StartedAt`, and configuration digest, so subsequent timer checks leave
later script-selected FPS and display metrics alone. It never starts stopped
devices. Explicit `--force` calls wait for any in-flight timer operation on each
selected device; background timer calls skip busy devices. A graphics restart
can restart Android apps, so resource changes belong
before launching a test. Native proxy reconciliation recovers the saved desired
policy after this restart, with no direct-network fallback. Pending network
recovery retries only the proxy, preserving later script display choices.

Original display backups are bound to the immutable native instance ID and
resource tag. Reusing a numeric device ID after deletion archives the prior QA
marker/backup before capturing the new original; Android data is untouched.
Upgrades retain and bind legacy backups. Host rollback prefers an identified
new-instance backup over unrelated dated records for the same numeric ID.

The installer also enables compressed swap via `xenoid-qa-zram.service` when no
administrator swap already exists. `/etc/default/xenoid-qa-zram` controls its
size, algorithm and priority. `auto` uses a logical size of half host RAM with
physical memory allocated only to stored compressed pages. Set
`ZRAM_SIZE_MIB=0` to disable creation on the next boot. Existing administrator
swap and configuration files are preserved during upgrades.

Before restoring display defaults, stop the shared low-resource script and set
`enabled` to `false` in the resource JSON; otherwise a future boot/configuration
change will apply the selected profile again. Then run `qa reconcile all --force`
(or Apply in the panel): when disabled, this waits for older resource operations
on the selected devices to finish and applies nothing. The installed
`/opt/xenoid-fleet/panel-scripts/restore-display.py` previews the restoration by
default (`--all` or `--device 1`); add `--apply` to perform it. Native composer
source and build/test instructions are included under `native/xenoid-hwcomposer`.

For an existing Fleet host, the verified source checkout also provides
`sudo -H bash install-qa-addon.sh`. This installs the addon and services without
building or recreating Android. `qa reconcile all` applies it to running devices.
It requires a previously installed pinned Fleet/upstream runtime.

### Official regeneration from a managed script

The Python SDK provides `Device.regenerate(timeout=0, operation_key=key,
post_cache=None)`. It
executes the pinned Xenoid `device regenerate` command under the same native
instance lock and waits for the official Android reload, profile and identity
verification. Container ID, immutable native instance ID, resource tag, Android
data volume and installed APK code stay bound to the same device. Android and
Google identities change according to Xenoid's official regeneration procedure.
The command retains the native single-user and readiness preconditions.

Use one operation key for a logical regeneration request, including every retry:

```python
import uuid
from xenoid_device import Device

device = Device()
operation_key = uuid.uuid4().hex  # Retain this value if the response is uncertain.
result = device.regenerate(timeout=0, operation_key=operation_key)
```

`timeout=0` has no overall deadline. A positive integer limits the request's
setup time. Host-side receipts persist before native journal transitions and
verify an already committed result after a lost response, so retrying the same
key resumes or confirms that operation within the same managed run/iteration.
A fresh key requests a new rotation. An unfinished operation is reported as
pending; the script must retain its key and wait rather than request another
rotation. Display settings are a separate script choice and should be reapplied
after the official profile replay.

To clear only selected packages' caches after the verified Android reload, pass
the plan on the first call and retain it with the operation key on every retry:

```python
post_cache = {"packages": ["com.google.android.gms", "com.google.android.gsf"],
              "user": "current", "timeout": 0}
result = device.regenerate(timeout=0, operation_key=operation_key,
                           post_cache=post_cache)
```

The SDK saves this plan before native work, fixes the Android user before the
first cache operation and checkpoints each confirmed package. The receipt is
committed only after all requested cache operations finish. A supervisor or
same-key retry resumes the saved native transaction and unfinished packages;
it does not request another identity rotation. A changed plan for the same key
is rejected. A cache failure remains `regeneration_post_cache_pending`, and a
completed replay clears nothing again. The plan accepts any installed Android
package, an explicit nonnegative user ID or `"current"`, and a cache timeout of
`null` (the saved panel command timeout), `0` (no package deadline) or a positive
integer. The overall regeneration deadline still applies.

If a worker restarts while a receipt is pending, startup keeps that run waiting
and retries the same operation key and saved cache plan before allowing another
app iteration. The panel run interval controls retries, including `0`; a finite
recovery budget is shared across attempts rather than reset on each retry.
Stop/supersession and immutable-device changes are checked between attempts.
An identity mismatch still fails closed. A transient pending result no longer
turns the worker permanently into a failed run.

`Device.clear_cache(package, user="current", timeout=None)` provides the same
standalone operation through Android's verified `cmd package clear --cache-only`
support. It requires a successful cache-only result and stable package UID;
there is no full-data deletion or alternate filesystem-delete fallback. Cache
cleanup preserves app data, accounts, preferences, Google IDs and native binding.
It is separate from `clear_data()`.

When an older native startup journal remains unfinished, the SDK may first
resume official `up` only after verifying that the remaining plan preserves the
same image, container and storage. It blocks image builds, container replacement,
storage migration and seed operations during this preparation. Native readiness
and proxy routing verification remain required; there is no direct-network
fallback. Internet diagnostics do not block a valid continuity route. A failed
ownership or structural acceptance check leaves the request pending.

An officially supported daemon-only live update can reuse the selected image
after verifying unchanged boot inputs, base/context/tool inputs, non-daemon
artifacts and storage, plus the native daemon signing/shared-user contract and
installed APK. This does not permit an input-driver update, image build or
container replacement during regeneration preparation.

Google process-exit history is scoped to the current Android userspace boot,
using the verified `system_server` start time. An ANR from an earlier Android
reload cannot permanently block a later regeneration. Fresh ANRs, crashes,
invalid clocks and a changing `system_server` identity still fail readiness;
the fix does not clear history or relax the native Google checks.

The bundled workflow draws one integer from `1..10` after a successful app
cycle and cleanup. Only `5` requests regeneration; it waits for completion,
clears the Google caches in its durable script-provided plan, restores the
script's display settings and begins the next cycle. Its editor
settings `REGENERATE_ENABLED`, `REGENERATE_RANDOM_RANGE`, `REGENERATE_TRIGGER`,
`REGENERATE_TIMEOUT` and `REGENERATE_RETRY_SECONDS` control this behavior. Failed
cycles do not draw. Existing saved scripts keep their own behavior until the
administrator selects a newer version.
Workflow version 13 defaults `GOOGLE_CACHE_PACKAGES` to
`("com.google.android.gms", "com.google.android.gsf")`. The package list, Android
user, cache timeout and retry delay are editable through `GOOGLE_CACHE_PACKAGES`,
`GOOGLE_CACHE_USER`, `GOOGLE_CACHE_TIMEOUT` and `GOOGLE_CACHE_RETRY_SECONDS`.
An empty package list disables this optional stage; other packages such as Play
Store are added only by the administrator. Nonmatching draws and failed QA cycles
do not clear Google caches.

Managed Bash scripts can use `device regenerate [TIMEOUT [OPERATION_KEY]]` or
`xenoid device regenerate [TIMEOUT [OPERATION_KEY]]` through the same SDK bridge.
For example, `device regenerate 0 after-cleanup` retains one key per managed
iteration. The ordinary host CLI remains
`xenoid-fleet run ID -- device regenerate [--dry-run]`.


## Run Xenoid commands

The arguments after `--` are sent to upstream Xenoid for the selected device or list of devices. Use the same coordinates and argument ordering as a direct upstream command.

The [complete command reference](docs/commands.md) covers the installer, Fleet selectors, every command and option in the pinned Xenoid CLI, and the separate service commands.

For a separate SSH coordinator and web panel, see the [Fleet control manager command reference](docs/control-manager.md).

```bash
sudo -H xenoid-fleet run 2 -- status
sudo -H xenoid-fleet run 2,3 -- input tap 540 1800
sudo -H xenoid-fleet run 2 -- input swipe 540 1600 540 400 500
sudo -H xenoid-fleet run 2 -- app install ./qa.apk
sudo -H xenoid-fleet run 2 -- app launch com.example.qa/.MainActivity
sudo -H xenoid-fleet run 2 -- adb shell getprop sys.boot_completed
sudo -H xenoid-fleet run all -- stop
sudo -H xenoid-fleet run all -- up
```

For an individual readiness check, run `sudo -H xenoid-fleet run 2 -- doctor --require-runtime`. Batch runs process IDs in order, continue after individual failures, and return a nonzero exit status if any command fails. Use one ID for interactive commands. Host-wide commands such as `build` can be run directly from the upstream checkout after sourcing `/opt/xenoid-fleet/toolchain/env.sh`. For command options and profile examples, see the [upstream command reference](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/docs/commands.md) and [operations guide](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/docs/operations.md).

Optional upstream operations such as graphical viewing and Frida use their own host tool dependencies.

The installed QA timer reapplies resource settings to registered running containers after a new boot. It does not start stopped devices. After a server restart, use `sudo -H xenoid-fleet run all -- up` to start the registered devices and reuse their data.

If `up` reports `proxy_disabled_state_unverified` for a running device, reconcile its saved proxy policy and retry `up`. For example, these commands recover device `1`.

```bash
sudo -H xenoid-fleet run 1 -- proxy reconcile
sudo -H xenoid-fleet run 1 -- up
```

## راهنمای سریع فارسی

روی سرور Ubuntu 24.04 با معماری ARM64، دستور بخش Install را اجرا کنید. برای ساخت دستگاه شمارهٔ ۲ از `sudo -H xenoid-fleet create 2` و برای ساخت یا بازسازی همان دستگاه با پروفایل از `sudo -H xenoid-fleet create 2 ./qa-profile.json` استفاده کنید. `list` وضعیت دستگاه‌ها را نشان می‌دهد. برای اجرای یک فرمان روی چند دستگاه، IDها را با کاما جدا کنید. برای نمونه، `sudo -H xenoid-fleet run 2,3 -- status` وضعیت دستگاه‌های ۲ و ۳ را می‌دهد. دستور `delete` داده‌های اندروید دستگاه‌های انتخاب‌شده را پاک می‌کند. اول با `--dry-run` مقصدها را بررسی کنید.

## Upstream

This repository provides the host installer and fleet CLI. The upstream [Xenoid project](https://github.com/Umiade/xenoid) provides the Android runtime and per-instance commands.


### Domain exceptions in the management panel

The Proxy page has an editable domain-exception list. Each row chooses an exact
DNS name or that name plus every subdomain. Save retains the policy for future
provisioning; Save and apply queues its native reconciliation across installed
hosts. Job failures remain visible and do not imply a successful apply. An empty
saved list disables exceptions and survives installer reruns.

The initial exceptions are `us.xdvpn.com`, `xdvpn.com`, `ir.xdgram.com`,
`xdgram.com`, and `github.com`, all with subdomains enabled. Redundant children
remain visible in the editor, while compilation reduces them to the three
parent suffix rules. Label boundaries are respected: `github.com.example.org`
and `notgithub.com` are not exceptions. `raw.githubusercontent.com` is a
separate domain and remains on the selected proxy unless explicitly added.

Fleet stores the root-owned policy at `/etc/xenoid-proxy-routing/policy.json`.
The native agent compiles the matching DIRECT rules ahead of `MATCH,GLOBAL`
and adds direct DNS resolution only for those exceptions. Existing encrypted
proxy sources, selected nodes and UDP/IPv6 policy flags are preserved. Direct
connections use the individual host's outbound address. An application's own
VPN can encapsulate its destinations inside a tunnel; host domain routing does
not independently select destinations hidden within that encrypted tunnel.

```sh
sudo -H xenoid-fleet proxy-routing show
sudo -H xenoid-fleet proxy-routing configure --json-stdin < routing.json
sudo -H xenoid-fleet proxy-routing reconcile all --timeout 180
```

The policy JSON uses schema `dev.xenoid.qa.proxy-routing/v1` and an `exceptions`
array of objects with `domain` and Boolean `include_subdomains`. Panel
provisioning stages its saved policy before create/start/recreate or enabling a
proxy. The policy lives outside Android data and is read again after native
regeneration. Installer defaults do not overwrite an existing custom policy.

### Shared-host memory tuning isolation

The stock Android display-memory producer repeatedly adds its requested reserve
to the current Linux `vm.watermark_scale_factor`. Privileged Android containers
share this host setting, so display/profile replay across devices can accumulate
large reservations and unnecessary reclaim/swap pressure.

The QA addon isolates only this verified producer. Existing guests receive a
read-only bind in Android init's mount namespace; stopped and newly created
instances receive the same producer patch inside their owned, committed ext4
root filesystem before Android starts. These are per-instance filesystem or
mount changes. They do not build a new Docker image or replace a current
container, data volume, APK, device identity or native touch driver.

An explicit migration restores the host's administrator-configured watermark,
or the Linux default when no override exists, only after every running registered
producer is verified guarded. No timer repeatedly resets the host value. The
original producer and pre-migration value are recorded privately for review.
The supplied host-memory rollback requires all managed containers and QA workers
to be stopped, verifies committed filesystem identities, and never recursively
removes a failed mount. The existing display/FPS rollback remains separate.

The SDK also reads the live native proxy engine/agent state before accepting an
Android cached success report. A dead host process triggers official saved-state
reconciliation; a successful return is checked against the actual generation and
runtime epoch and policy digest. In continuity mode a valid route remains ready
while upstream diagnostics are unknown or retrying; display setup does not
repeatedly reconcile merely because the internet is unavailable.

### Temporary proxy and control-channel failures

Fresh/default policies use `failure_policy` on the existing domain-routing profile:

```json
{"schema":"dev.xenoid.proxy-failure-policy/v1","mode":"retry_proxy","retry_delays_seconds":[5,15,60]}
```

The Proxy page exposes this opt-in and editable retry delays. Existing saved
policies without the field keep legacy behavior until explicitly migrated.
Delays are positive integer seconds; they control local engine/source recovery,
not a background internet gate. The five saved domain exceptions are retained.

In continuity mode, provider limits, disconnects and permanent upstream failure
keep the configured proxy enabled and transparent routes installed. All
non-exempt requests still go through that proxy; no automatic DIRECT fallback,
source-off toggle, replacement source or external-probe quarantine is added.
The agent repairs a clearly owned dead Mihomo process with configured backoff,
without rebooting Android, changing device identity or replacing official touch.
A previously valid subscription configuration is retained during fetch outages.

`proxy status --check` is an optional manual diagnostic. A failed check reports
its real error without disrupting a structurally valid route or blocking GEN.
`upstreamState` describes the last explicit diagnostic (`unknown`, `online`, or
`retrying`), not continuous monitoring. `dataPlaneVerified` remains false when
no successful proof exists. Device readiness does not imply working internet:
a dead upstream cannot carry requests, so app requests may wait or fail while
the device and scenario keep running and retrying.

Malformed responses, unreadable credentials, authentication/ownership failures,
wrong generation/runtime and invalid structural state still fail closed. A
provider outage is kept separate from those security and configuration faults.

Version v0.1.22 also sets the mode of a newly created public domain-policy directory explicitly, so installation and routing configuration work under a private service umask. An existing private directory is still rejected; Native private settings permissions are unchanged. Existing custom and empty routing policies are retained.

Version v0.1.23 makes repeated cleanup of owned proxy networking resources idempotent inside an existing network namespace. It checks the inner IP resource in that same namespace; an absent resource is accepted only after the matching readback proves absence. Permission, namespace-entry and unrelated failures still fail the operation. This changes no proxy source, input backend, Android image or device identity.


After a successful cycle draws 5, workflow18 completes official Xenoid regeneration,
clears the configured Google caches, waits2seconds, clears the full app data of
`com.google.android.gms`, waits2seconds, clears `com.google.android.gsf`, waits2seconds,
and enables microG Device registration through its supported ProvisionService.
The package list, delays and operation timeout are editable scenario settings.
The post-stage retires only the authenticated obsolete offline GSF marker after the requested data clear, handing identity management back to microG without changing native guards. The receipt checkpoints native completion and each post-action independently;
recovery resumes unfinished steps without another regeneration or a data clear
after registration has begun. Registration-switch proof is independent of
Google-side network check-in completion. The latter depends on connectivity.
Before the next official regeneration, the recorded previous native Google
baseline is restored through the official daemon API so the native offline
identity checks remain intact; registration is enabled again at the end of that
regeneration's post-actions. Existing cache-only scripts and saved revisions
retain their prior behavior. Full data reset applies only when explicitly
requested by the new scenario plan. Official native touch is unchanged.


### Container memory reclaim headroom (v0.1.34)

The resource policy accepts `memory_reserve_mib`. A fresh policy uses `256`.
An existing fresh native `render_fps=15` policy also resolves a missing field
to `256`; legacy display policies without native pacing keep `0`. An explicit
`0` disables this reserve. For the qualified 1 CPU,
2 GiB RAM and 512 MiB swap profile, `256` sets the cgroup-v2 soft reclaim
threshold `memory.high` to 1792 MiB. The hard CPU, memory and swap limits stay
unchanged. Configure the same field through the management panel, then apply
resources to the selected devices. Existing Fleet post-start and periodic
resource reconciliation retain this setting after a new container starts.

The helper records the original value before writing and checks the exact
runtime and cgroup. A conflicting administrator setting is rejected. Set the
field to `0` to restore the owned original value; an unowned limit is untouched.
A bounded child writer is reaped before its owner releases the operation.
Unsupported container limits fail explicitly when the reserve is enabled.
This is an earlier reclaim threshold, not extra memory or an Android LMKD fix;
an app whose working set exceeds the hard limit can still run out of memory.

## Per-device JSON profiles

The management panel captures each device's authoritative
`/data/local/tmp/xenoid-profile/effective.json` as its immutable baseline.
The profile policy is versioned in the panel and published to current and new
registered hosts as `/etc/xenoid-fleet/device-profile-policy.json`.

After a selected official regeneration, the worker pins one generated full JSON
and applies it through the native `device apply PROFILE --instance-identity`
handler. An additional native userspace reload refreshes cached Build values;
core regeneration IDs are preserved. Display dimensions and actual composer FPS
then follow the applied profile. Retries reuse the same target and operation key.
The existing Google cache/data/reset-registration stages run after profile verification.

The panel shows checkboxes and editable candidate arrays for supported dynamic
fields, with deterministic `{token}` / `{digits}` placeholders for supported text
metadata. Coupled values are validated together. Original hardware/OS fields
that this runtime compiles into native components are read-only with an explicit
reason; native-managed IDs continue to rotate through official regeneration.
The profile editor exposes reported Build, CPU and RAM/swap values with native
JSON consumers, while separating editable fields, preset-derived display fields
and fixed/native-managed values. Build candidates preserve property
storage capacity. After the official reload, each selected field has a live
consumer audit: applied, partial, unchanged, mismatched or unobservable. A
successful native command alone does not prove that a getter changed. The
worker requires a real observed change when randomization is selected and
retains native property warnings. Root/shell observations do not establish what
every cached app API reports. No additional native shim is installed.
Reported CPU/RAM values do not alter the host's physical resources; the existing
display/FPS and host memory optimizations remain in place.
A changed JSON value does not assert OEM signing, physical hardware changes, or
hardware-backed DRM certification.

### Android regional, font and theme preferences

`qa/vpn-cycle.py` exposes `UI_PREFERENCES` in the shared script editor. Its
default selects `en-US`, one of four US time zones, font scales `0.85`, `1.0`,
`1.15`, and light/dark mode. New native devices initialize in the US. The
default `native_location` request moves existing devices to the US during
their next selected official GEN and rotates among T-Mobile (`310260`),
AT&T Mobility (`310410`) and Verizon Wireless (`311480`). The journal owns
the complete SIM, carrier/APN and regional target before any identity write.
The same Android container, installed APK and resource limits are retained.

Supply a `regions` array to limit the choices; entries use `locales`,
`timezone`, `time_12_24` and `date_format`. Omit `native_location` to change
only user preferences. Neither mode changes Build fields, display dimensions,
DPI or frame pacing. Mobile-network metadata does not change proxy egress IP.

The worker captures the original settings and chooses the target before GEN.
It applies the chosen user preferences after native/profile verification and
before the existing Google cleanup/registration phases. A durable operation
receipt fixes the selection across retries. Native Location replay respects
the explicit user locale/time-zone marker while retaining its other identity
checks. A failed preference apply restores the captured settings before retry.

To undo this feature, set `UI_PREFERENCES = None` in the shared scenario and run
`qa/restore-device-region.py` once on the desired devices through the panel.
It uses a new official GEN to restore the original native country with fresh
identifiers, then restores the original UI settings, including absent keys.
`qa/restore-ui-preferences.py` remains available for UI-only rollback.
Original state is kept privately under
`/var/lib/xenoid-fleet/ui-preferences/` with the instance binding. Do not delete
that directory while rollback is needed.

Apps can choose their own language, theme or text sizing. Android preference
readback proves the system setting; a browser or app must be checked separately
to establish which preferences it honors.

### Random display dimensions and atomic presets (v0.1.28)

New profile policies select a random width from a bounded range around a stable
configured base. Height and DPI are derived together; the base does not move
after each round. The default policy uses:

```json
{
  "display_random": {
    "enabled": true,
    "base_width": 200,
    "base_height": 356,
    "min_width": 200,
    "max_width": 220,
    "width_step": 2,
    "base_dpi": 72,
    "fps": 20
  }
}
```

The width is chosen uniformly from `min_width + n * width_step <= max_width`.
Whenever another size exists, the immediately previous width/height is excluded.
The operation key fixes the choice for retries. Height is rounded to the nearest
even pixel, and DPI to the nearest integer; exact ties round upward. DPI is then
clamped to Android's minimum of 72. Thus the current default range is 200×356
at 72 DPI through 220×392 at 79 DPI, always at 20 FPS, without a random walk.
Android 13's `DisplayContent.setForcedSize` clamps either dimension below 200
pixels. The requested 180-pixel lower bound is therefore unsupported by this
official path; it is rejected before regeneration instead of silently applying
a different size. A larger configured base can have a range both above and
below it, provided both applied dimensions remain at least 200. Every reachable
tuple is validated before native regeneration starts.

The optional `display_presets` field remains available for explicit tuples and
existing saved policies. It is mutually exclusive with `display_random`:

```json
{
  "display_presets": {
    "enabled": true,
    "values": [
      {"width": 200, "height": 356, "dpi": 72, "fps": 20},
      {"width": 204, "height": 362, "dpi": 72, "fps": 20},
      {"width": 208, "height": 370, "dpi": 74, "fps": 20},
      {"width": 212, "height": 376, "dpi": 76, "fps": 20},
      {"width": 216, "height": 384, "dpi": 76, "fps": 20}
    ]
  }
}
```

Administrators can edit range parameters or explicit presets in the panel.
Applied width and height must be integers from 200 through 32767, DPI from 72 through
65535, and FPS from 1 through 1000000000; Android and
the official native runtime must still accept and verify the chosen values.
The DPI lower bound is enforced by Android 13's
[WindowManager shell command](https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r1/services/core/java/com/android/server/wm/WindowManagerShellCommand.java),
not a panel resource policy. The dimension lower bound likewise comes from
[DisplayContent.setForcedSize](https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r1/services/core/java/com/android/server/wm/DisplayContent.java).
Historical profile documents remain readable.
Duplicate presets and an enabled empty list are rejected. Two or more distinct
sizes allow size variation; one preset deliberately fixes the display.

For each new profile operation, selection is deterministic from the saved
operation key and policy. It prefers a different width/height pair from the
current one whenever available, then a different complete tuple. The selected
tuple is written into the full native JSON. Both refresh-rate fields receive
the selected FPS; input X becomes `minimum=0, maximum=width-1`, and input Y becomes
`minimum=0, maximum=height-1`. Independent scalar display/input choices remain
stored in the policy but are inactive while this group is present. There is no
second set of active touch coordinates to edit.

The panel offers four explicit modes:

- **Random range:** `display_random.enabled=true` derives a full tuple from a
  uniformly selected width and the configured base aspect ratio.
- **Presets:** `display_presets.enabled=true` selects and applies a tuple.
- **Freeze:** a present group with `enabled=false` preserves the current complete
  display/input tuple; it does not reactivate the retained scalar choices.
- **Legacy:** omit both groups to use the previous independent field settings.

Reading an existing policy preserves its current range, presets or legacy mode. The
upgrade button prepares a draft; only an explicit save publishes a new policy
revision, such as revision 3 to 4. Existing custom presets are retained by the
upgrade; it adds a random range only when neither display group exists. A pending
operation keeps its already pinned policy and target JSON. The result records
the `display_source` and exact chosen `display_selection` tuple.

Before publishing a successful profile, the worker verifies the effective JSON,
WindowManager dimensions/DPI, actual composer period, official input status and
kernel touch axes. It checks nine logical-to-raw mappings without sending touch
events. Rotation is accounted for: InputReader physical dimensions need not be
identical to rotated logical dimensions. The resulting `display_input` proof is
included in the per-field audit. Failed geometry verification leaves the same
operation pending. All scenario touches continue through Xenoid's official
native input path; no ADB touch fallback or new native library is introduced.

Display values in the committed profile are the resource reconciler's source
for that device while profile management is enabled. Devices without an applied
profile continue to use the central resource defaults. Reported CPU and RAM
changes do not allocate different physical host resources.

### Fingerprint uniqueness scope

Default candidate selection avoids the immediately previous fingerprint when
the enabled, linked fields provide a compatible alternative. Disabling a linked
field or supplying only unchanged/incompatible candidates can prevent that
change. The generator does not maintain a global set of fingerprints used by
other devices or earlier rounds. An older value, including the original
baseline fingerprint, can be selected again. Recorded history is audit data,
not a uniqueness reservation.

The default fingerprint templates reuse one six-digit decimal value in their
Build ID/incremental components; these are not independent random values. The
optional `{token}` placeholder is a separate 16-character hexadecimal rendering
of the deterministic seed, but it is not used by the default fingerprint
templates. JSON selection and actual Android getter observations are reported
separately; neither a saved candidate nor a native command's success flag proves
that every app API changed.
