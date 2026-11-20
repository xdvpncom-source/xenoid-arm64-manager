# Xenoid ARM64 Fleet

Install Xenoid on an ARM64 Ubuntu server and manage headless Android 13 instances through a local CLI. Each numeric ID maps to an upstream instance name. For example, `2` selects `phone-2`. Xenoid allocates the container, persistent volume, network, and loopback ADB port for each instance.

## Install

Use an **ARM64 (aarch64) server running Ubuntu 24.04**, such as a Hetzner CAX server. The installer requires root access, outbound HTTPS, and free disk space for the build toolchain, container images, and Android data. Install and management commands use the same operator account and Xenoid registry.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.13/install.sh | sudo -H bash -s -- --accept-android-licenses
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
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.13/install.sh | sudo -H bash -s -- --install-only --accept-android-licenses
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

### Panel-controlled QA defaults

Version v0.1.13 installs the QA SDK/backend, the native logical-to-raw input
coordinate correction, and configurable resource defaults for all instances,
including devices with no APK or shared script assignment. The stock pinned
Redroid image, each native instance identity, persistent data, and saved proxy
source/node/policy remain in place. The addon mounts its verified ARM64 composer
over the running container's vendor path; it does not replace the stock image.
Resource transactions wait for the native instance lock using the configured
timeout (`0` waits without a deadline), then hold it through FPS and display
writes. Normal script input/ADB lock behavior stays unchanged. Failure reports
include a safe native reason code or exception class, never raw exception text.

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
the [`vpn-cycle-v12.py` release asset](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.13/vpn-cycle-v12.py)
with a [SHA-256 sidecar](https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/v0.1.13/vpn-cycle-v12.py.sha256).
The historical v0.1.12 `vpn-cycle-v11.py` asset remains unchanged. Updating the
editable workflow never rewrites an existing release tag or its installer pins.

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

The Python SDK provides `Device.regenerate(timeout=0, operation_key=key)`. It
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

When an older native startup journal remains unfinished, the SDK may first
resume official `up` only after verifying that the remaining plan preserves the
same image, container and storage. It blocks image builds, container replacement,
storage migration and seed operations during this preparation. Native readiness
and proxy data-plane verification remain required; there is no direct-network
fallback. A failed acceptance check leaves the request pending.

The bundled workflow draws one integer from `1..10` after a successful app
cycle and cleanup. Only `5` requests regeneration; it waits for completion,
restores the script's display settings and begins the next cycle. Its editor
settings `REGENERATE_ENABLED`, `REGENERATE_RANDOM_RANGE`, `REGENERATE_TRIGGER`,
`REGENERATE_TIMEOUT` and `REGENERATE_RETRY_SECONDS` control this behavior. Failed
cycles do not draw. Existing saved scripts keep their own behavior until the
administrator selects a newer version.

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
