# Xenoid ARM64 Fleet

Install Xenoid on an ARM64 Ubuntu server and manage headless Android 13 instances through a local CLI. Each numeric ID maps to an upstream instance name. For example, `2` selects `phone-2`. Xenoid allocates the container, persistent volume, network, and loopback ADB port for each instance.

## Install

Use an **ARM64 (aarch64) server running Ubuntu 24.04**, such as a Hetzner CAX server. The installer requires root access, outbound HTTPS, and free disk space for the build toolchain, container images, and Android data. Install and management commands use the same operator account and Xenoid registry.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.26/install.sh | sudo -H bash -s -- --accept-android-licenses
```

The `--accept-android-licenses` flag accepts the Android SDK licenses for the pinned build tools from Google. The installer prepares the host and starts device `1`. Allow tens of minutes for the first build on a small server. The CLI is installed as `/usr/local/bin/xenoid-fleet` and the managed checkout lives under `/opt/xenoid-fleet/upstream`. Rerunning the installer reuses the toolchain and converges device `1` with its existing data.

Android runs natively on ARM64 under Docker. An existing Docker engine needs Buildx and the containerd image store, which is the default on new Docker Engine 29 installations. The installer enables x86-64 userspace emulation for Google's Linux SDK and NDK executables. The Xenoid build cache and Docker images stay on the server for subsequent devices. The fleet CLI gives image builds separate Buildx client state so Xenoid can verify their payloads against the current build inputs.

The installer loads the IPv4 and IPv6 kernel netfilter tables that Xenoid checks during startup. It verifies and applies patches to the pinned Xenoid source. The standalone daemon build supplies the same package and version metadata as Gradle and reads Java source as UTF-8. The KeyMint build remaps paths from the installed Rust toolchain before artifact validation. The shared runtime inventory inspects containers individually so large fleets fit within the command output limit while retaining the same ownership checks.

Check that Android booted. The command output includes the value `1` after boot completes.

```bash
sudo -H xenoid-fleet run 1 -- adb shell getprop sys.boot_completed
```

Use `--install-only` to prepare the host before creating the first device or upgrade an existing installation without issuing a device start. Upgrades also refresh enabled proxies on already-running devices when their native control/process revision has not been verified.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.26/install.sh | sudo -H bash -s -- --install-only --accept-android-licenses
```

## Devices

| Action | Command |
| --- | --- |
| List devices and runtime status | `sudo -H xenoid-fleet list` |
| List devices as JSON | `sudo -H xenoid-fleet list --json` |
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

Create the root-owned directory `/opt/xenoid-fleet/proxy-default` with mode `0700`. Use the native `proxy export --out` command on a configured instance to create `source.conf`. Keep that file and `manifest.json` root-owned with mode `0600`. The manifest contains `schema` set to `dev.xenoid.fleet.proxy-default/v1`, the exported `kind`, the hex `sha256` of `source.conf`, and the Boolean policy fields `udpAllowed`, `ipv6Allowed`, and `allowInsecureHttp`.

After startup, Fleet imports this source through the native Xenoid CLI on unconfigured devices and requires a successful data-plane check. Create, recreate, and plain `up` keep a device's configured proxy policy and use Xenoid's native reconciliation.

The pinned Xenoid patch reads live host quarantine status during boot acceptance. The native acceptance checks still require a verified data plane before accepting an enabled proxy.

The patch also preserves the rules and traffic counters already verified during
candidate staging when committing that candidate. Commit checks the pending
process, health, current structure and runtime identity before promoting its
metadata. Acceptance reads fresh host counters after the daemon report and
continues to require matching generation, runtime epoch, capabilities and
counter proof. Stale snapshots cannot accept an unverified or reset data plane.
Startup planning uses that same combined daemon/host proof, allowing a healthy
enabled proxy to converge instead of remaining permanently pending on the
host-only disabled-state flag.

The installer refreshes each running, configured, enabled proxy through native
prerequisite, quarantine, agent stop and desired-state reconciliation. It checks
the original container, instance identity, data mount and complete source/policy
fingerprint, then requires strict live proxy proof. It preserves the selected
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

### Panel-controlled QA defaults

Version v0.1.26 installs the QA SDK/backend, the native logical-to-raw input
coordinate correction, and configurable resource defaults for all instances,
including devices with no APK or shared script assignment. The stock pinned
Redroid image, each native instance identity, persistent data, and saved proxy
source/node/policy remain in place. The addon mounts its verified ARM64 composer
over the running container's vendor path; it does not replace the stock image.
Resource transactions wait for the native instance lock using the configured
timeout (`0` waits without a deadline), then hold it through FPS and display
writes. Normal script input/ADB lock behavior stays unchanged. Failure reports
include a safe native reason code or exception class, never raw exception text.

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

The default profile is `/etc/xenoid-fleet/qa-resources.json`:

```json
{"enabled":true,"fps":20,"scale":"minimum","min_side":200,"dpi":72,"timeout":180}
```

Every value is editable through the management panel or JSON configuration.
`fps: null` preserves the current rate; `0` restores the native modes. `scale:
null` preserves the display metrics; `1` restores physical metrics; a positive
number scales the physical metrics. `"minimum"` keeps the physical aspect ratio,
rounds dimensions to even pixels, and uses `min_side` and `dpi`. For a physical
720×1280 display, the defaults produce 200×356 at 72 DPI. Positive administrator
values are passed to Android and verified by readback; unsupported values fail
with rollback. `timeout: 0` permits unlimited setup time. These are resource
defaults, with no action/package/ADB/round policy added to scripts.

The panel supplies each device's APK assignment and saved script version.
Installing the addon does not select an app, clear app data or begin a QA cycle
by itself. Workflow revisions are independent of Fleet installer releases:
the panel's saved version governs execution on existing and newly assigned
devices, so changing ad selectors or timing does not require a Fleet upgrade.

`qa/vpn-cycle.py` is the release-time reference protected by the installer
manifest. The current panel workflow is also mirrored in
[`examples/current-vpn-cycle.py`](examples/current-vpn-cycle.py) and exported as
the [`vpn-cycle-v19.py` release asset](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.26/vpn-cycle-v19.py)
with a [SHA-256 sidecar](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.26/vpn-cycle-v19.py.sha256).
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
and proxy data-plane verification remain required; there is no direct-network
fallback. A failed acceptance check leaves the request pending.

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
runtime epoch. Display readiness and proxy network readiness remain separate.

### Temporary proxy and control-channel failures

Provider connection limits and temporary transport failures leave Android and
the QA scenario's saved progress intact. The native proxy agent retains its
authenticated session and retries recoverable connection, timeout and local
port/file-descriptor exhaustion errors with interruptible backoff. A pending
failure stays visible until a fresh Android network probe succeeds, including
when the desired configuration and check ID have not changed. The agent never
reports an old successful probe as current recovery.

Malformed responses, authentication/ownership failures and missing structural
control files still fail explicitly. Recovery does not reboot or regenerate a
device, alter the selected proxy source, or replace official Xenoid touch.
While a provider remains unavailable, network-dependent actions can remain
pending; the panel cannot remove an upstream provider's connection quota.

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
fields, with deterministic `{token}` / `{digits}` placeholders for QA build
metadata. Coupled values are validated together. Original hardware/OS fields
that this runtime compiles into native components are read-only with an explicit
reason; native-managed IDs continue to rotate through official regeneration.
QA-generated fingerprints do not assert OEM signing, physical hardware changes,
or hardware-backed DRM certification.
