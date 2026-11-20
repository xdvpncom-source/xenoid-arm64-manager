# Xenoid Fleet command reference

This reference covers Fleet `v0.1.20` and the Xenoid CLI at commit `c07e56b4aef8cd7e4d56eed11c84781369d29997`. Use `-h` or `--help` at each command level to see the installed parser's help. Brackets mark optional arguments. Uppercase words in syntax are values to supply. [Fleet launcher](../xenoid-fleet) · [Pinned Xenoid parser](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/src/xenoid/cli.py#L4058)

برای دستگاه اول `run 1 --`، برای چند دستگاه `run 1,3 --` و برای همهٔ دستگاه‌های عددی این میزبان `run all --` را قبل از فرمان Xenoid بگذارید. مثال‌های ساخت، حذف، خواندن مشخصات و گرفتن عکس در همین صفحه آمده‌اند.

## Install

The installer requires native ARM64 Ubuntu 24.04 and starts device `1` after installation. `--accept-android-licenses` accepts the Android SDK licenses during the installation. `--install-only` prepares the host so device creation can be scheduled separately.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.20/install.sh | sudo -H bash -s -- --accept-android-licenses
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.20/install.sh | sudo -H bash -s -- --accept-android-licenses --install-only
bash install.sh --help
```

`install-toolchain.sh ABSOLUTE_TOOLCHAIN_PREFIX` is the host toolchain installer called by `install.sh`. Its `-h` and `--help` list `XENOID_ACCEPT_ANDROID_SDK_LICENSES`, `XENOID_ANDROID_SDK_ROOT`, and `XENOID_REUSE_RUSTUP`. Use the complete installer above for an ordinary Fleet setup. [Installer](../install.sh) · [Toolchain installer](../install-toolchain.sh)

## Fleet commands

Use `sudo -H` for the root registry created by the installer. Numeric ID `2` selects upstream instance `phone-2`. IDs are positive decimal integers without leading zeroes, with up to 26 digits. A comma-separated selector preserves argument order and accepts each ID once. The `all` selector targets registered numeric instances on this host in numeric order.

| Syntax | Example | Result |
| --- | --- | --- |
| `xenoid-fleet --version` | `xenoid-fleet --version` | Show the installed Fleet release. |
| `xenoid-fleet list [--json]` | `sudo -H xenoid-fleet list --json` | Show per-device runtime status and recommended action. The JSON result has a top-level `ok` and `devices` array. |
| `xenoid-fleet create ID[,ID...] [PROFILE] [--no-google-services]` | `sudo -H xenoid-fleet create 1,2 ./qa-profile.json` | Create or recreate these devices, validate readiness, then apply the optional fingerprint profile. An existing Android data volume is retained. |
| `xenoid-fleet delete SELECTOR [--dry-run \| --yes]` | `sudo -H xenoid-fleet delete 2,3 --dry-run` | Preview by default. `--yes` releases the owned containers and permanently deletes their volumes. |
| `xenoid-fleet run SELECTOR -- COMMAND [ARG ...]` | `sudo -H xenoid-fleet run all -- status` | Pass the remaining arguments to upstream Xenoid with the matching instance selected. |
| `xenoid-fleet qa configure --json-stdin` | `sudo -H xenoid-fleet qa configure --json-stdin < qa-resources.json` | Atomically store panel-controlled resource defaults. |
| `xenoid-fleet qa reconcile SELECTOR [--force]` | `sudo -H xenoid-fleet qa reconcile all --force` | Apply defaults to running devices; force explicitly reapplies a previously marked boot. |
| `xenoid-fleet qa upgrade-proxy SELECTOR [--timeout SECONDS]` | `sudo -H xenoid-fleet qa upgrade-proxy all --timeout 180` | Refresh enabled running proxies to the installed native control revision and verify the original source/binding. Zero permits an unlimited lock/setup wait. |

```bash
sudo -H xenoid-fleet --help
sudo -H xenoid-fleet create 2
sudo -H xenoid-fleet create 3 --no-google-services
sudo -H xenoid-fleet list
sudo -H xenoid-fleet run 2 -- status
sudo -H xenoid-fleet run 1,2 -- adb shell getprop sys.boot_completed
sudo -H xenoid-fleet delete 3 --dry-run
sudo -H xenoid-fleet delete 3 --yes
```

- [QA resource defaults](../README.md#panel-controlled-qa-defaults) apply after create/recreate and waited start/up. Dry-run operations skip the addon. The boot timer handles new running container boots without changing a later script-selected display on the same boot.
- `create` accepts an explicit ID or list. Use `--no-google-services` at creation. Provider selection is bound when Android data is created.
- `create` and plain `run ID -- up` apply a configured private Chrome baseline after native startup. [Chrome baseline setup](../README.md#optional-chrome-baseline)
- `run` and `delete` accept an explicit ID, list, or `all`. Batch execution processes devices sequentially, continues after an individual failure, and exits nonzero if any selected operation fails. A single-device `run` returns the upstream exit code.
- `list` waits for each registered device's upstream `status` before it prints the table. Use `run 2 -- status` to check just one device.
- `run` maps the global `--instance` option itself. Put each command's own flags after its command name. For example, `sudo -H xenoid-fleet run 2 -- doctor --require-runtime` checks the live runtime.
- Use a single instance for graphical `view`, interactive ADB `shell`, or stdin-driven proxy configuration. For terminal prompts, use an interactive terminal. For a batch that needs one output file per device, run each ID with a different output path.

## Direct checkout commands

Fleet owns numeric `init` and `delete` through `create` and `delete`. Run `build`, `install-runtime`, `instance`, `linux-binderfs`, `package-release`, `verify-release`, and shared eBPF maintenance in the checkout. Fleet accepts `ebpf status` for scoped viewing.

On a managed ARM64 host, enter the checkout in the registry owner's shell.

```bash
sudo -H bash
source /opt/xenoid-fleet/toolchain/env.sh
cd /opt/xenoid-fleet/upstream
./xenoid --version
./xenoid instance list
./xenoid --instance phone-2 status
```

The direct CLI selects `--instance NAME`, then `XENOID_INSTANCE`, then `default`. `--instance`, `--version`, and `-h` or `--help` are global options. Direct examples with `--instance phone-2` below refer to a registered device. For each device-scoped command, the equivalent Fleet form is `sudo -H xenoid-fleet run 2 --` followed by the command and its arguments. [Pinned operator guide](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/docs/operations.md)

### Instances, configuration, and lifecycle

| Syntax | Example |
| --- | --- |
| `install-runtime [--dry-run]` | `./xenoid install-runtime --dry-run` on macOS with Colima |
| `linux-binderfs [--dry-run]` | `./xenoid linux-binderfs --dry-run` on Linux |
| `instance list` | `./xenoid instance list` |
| `init [--image IMAGE] [--backend BACKEND] [--config CONFIG] [--from FROM_INSTANCE] [--no-google-services]` | `./xenoid --instance qa-test init --config examples/config-linux-arm.json --no-google-services` |
| `delete [--dry-run]` | `./xenoid --instance qa-test delete --dry-run` |
| `config show` | `./xenoid --instance phone-2 config show` |
| `config set [--backend BACKEND] [--image IMAGE] [--runtime-image-tag TAG] [--docker-context NAME] [--network-dns-server ADDRESS ...] [--auto-build-runtime-image \| --no-auto-build-runtime-image]` | `./xenoid --instance phone-2 config set --auto-build-runtime-image` |
| `up [--dry-run] [--skip-build]` | `./xenoid --instance phone-2 up --dry-run` |
| `start [--dry-run] [--no-wait] [--start-colima] [--install-daemon APK] [--no-adb-root] [--skip-preflight] [--recreate]` | `./xenoid --instance phone-2 start --dry-run` |
| `stop` | `./xenoid --instance phone-2 stop` |
| `status` | `./xenoid --instance phone-2 status` |
| `doctor [--full] [--require-runtime] [--out PATH]` | `./xenoid --instance phone-2 doctor --require-runtime --out ./phone-2-doctor.json` |
| `logs [--out-dir DIR]` | `./xenoid --instance phone-2 logs --out-dir ./phone-2-logs` |
| `view` | `./xenoid --instance phone-2 view` on a host with a graphical session and scrcpy |
| `adb [ADB_COMMAND ...]` | `./xenoid --instance phone-2 adb shell getprop ro.product.model` |

Use `up` for normal startup and convergence. `start` is lower-level runtime control. `up --skip-build` validates existing build records. `start --start-colima` applies to configurations using Colima. `config set --network-dns-server` may be repeated to set the resolver list. A `doctor --full` run also builds artifacts and runs the extended checks. Direct `delete` without `--dry-run` performs the deletion.

### ADB, screenshots, and live view

The ADB subcommand receives the remaining arguments from Xenoid. Fleet supplies the selected transport. Use `adb shell getprop` for Android properties or the device profile commands below for a structured report. The installed `adb` provides its own subcommands and flags.

```bash
sudo -H xenoid-fleet run 1 -- adb shell getprop ro.product.model
sudo -H xenoid-fleet run 1 -- adb shell getprop
sudo -H xenoid-fleet run 1 -- adb shell wm size
sudo -H xenoid-fleet run 1 -- adb devices -l
sudo -H xenoid-fleet run 1 -- adb connect
adb help
```

Fleet reserves ADB server and device selection. Its forwarded ADB excludes `attach`, `detach`, `disconnect`, `host-features`, `kill-server`, `mdns`, `pair`, `reconnect`, `server`, `start-server`, `track-devices`, and `wait-for-any-*`. The pinned direct Xenoid CLI also rejects `adb kill-server`. For a raw PNG, read the reported ADB target and use the host's `adb` so the image remains binary.

```bash
adb_target=$(sudo -H xenoid-fleet run 1 -- status | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance"]["adbTarget"])')
sudo -H adb -s "$adb_target" exec-out screencap -p > ./phone-1.png
sudo -H xenoid-fleet run 1 -- view
```

`view` opens scrcpy on the machine running the command and needs a graphical session there. Capture uses the host ADB session and accepts one transport per PNG.

### Image, build, release, and engine protection

| Syntax | Example |
| --- | --- |
| `runtime-build-image [--image IMAGE] [--dry-run]` | `./xenoid --instance phone-2 runtime-build-image --dry-run` |
| `runtime-context [--image IMAGE]` | `./xenoid --instance phone-2 runtime-context` |
| `make-rootfs` | `./xenoid --instance phone-2 make-rootfs` |
| `build daemon` | `./xenoid build daemon` |
| `build keymint` | `./xenoid build keymint` |
| `build input` | `./xenoid build input` |
| `build hide` | `./xenoid build hide --help` |
| `build profile` | `./xenoid build profile` |
| `build netctl` | `./xenoid build netctl` |
| `build gralloc` | `./xenoid build gralloc` |
| `build hwcomposer` | `./xenoid build hwcomposer` |
| `build ril` | `./xenoid build ril` |
| `build radio-config` | `./xenoid build radio-config` |
| `build all [--force]` | `./xenoid build all` |
| `package-release [--version VERSION]` | `./xenoid package-release --version 0.9.3` |
| `verify-release ARCHIVE` | `./xenoid verify-release ./xenoid-release.tar.gz` |
| `ebpf build` | `./xenoid --instance phone-2 ebpf build` |
| `ebpf load` | `./xenoid --instance phone-2 ebpf load --help` |
| `ebpf status` | `./xenoid --instance phone-2 ebpf status` |
| `ebpf smoke` | `./xenoid --instance phone-2 ebpf smoke` |
| `ebpf unload [--maintenance]` | `./xenoid --instance phone-2 ebpf unload --help` |

`build all --force` rebuilds the targets and verifies deterministic output. eBPF load and unload affect the shared engine host. Unload requires the maintenance acknowledgement and no active Xenoid runtimes.

### Google services and daemon

| Syntax | Example |
| --- | --- |
| `google-services releases` | `./xenoid google-services releases` |
| `google-services status [--require-runtime]` | `./xenoid --instance phone-2 google-services status --require-runtime` |
| `google-services import-mindthegapps ARCHIVE CERTIFICATE` | `./xenoid --instance phone-2 google-services import-mindthegapps ./release.zip ./release.x509.pem` |
| `google-services import-microg GMSCORE GSFPROXY` | `./xenoid --instance phone-2 google-services import-microg ./GmsCore.apk ./GsfProxy.apk` |
| `google-services enable [--release RELEASE]` | `./xenoid --instance qa-test google-services enable` before first Android data creation |
| `google-services disable` | `./xenoid --instance qa-test google-services disable` before first Android data creation |
| `daemon health` | `./xenoid --instance phone-2 daemon health` |
| `daemon install APK` | `./xenoid --instance phone-2 daemon install ./daemon/app/build/outputs/apk/debug/app-debug.apk` |
| `daemon start` | `./xenoid --instance phone-2 daemon start` |
| `daemon ensure` | `./xenoid --instance phone-2 daemon ensure` |

`google-services enable --release` accepts `microg-0.3.15.250932-phonesky-30.4.17-gsfproxy-0.1.0` in this pin. Imports verify the pinned releases and their expected files. Check `google-services releases` before choosing an asset.

### Device reports and profiles

| Syntax | Example |
| --- | --- |
| `device collect [--out PATH]` | `./xenoid --instance phone-2 device collect --out ./phone-2-profile.json` |
| `device apply [--keep-unique \| --instance-identity] [--generate-frida] [--frida-out PATH] PROFILE` | `./xenoid --instance phone-2 device apply --instance-identity ./qa-profile.json` |
| `device set FIELD VALUE` | `./xenoid --instance phone-2 device set --help` |
| `device regenerate [--dry-run]` | `./xenoid --instance phone-2 device regenerate --dry-run` |
| `device generate-frida [--out PATH] [--keep-unique] PROFILE` | `./xenoid --instance phone-2 device generate-frida --help` |
| `device generate-service-frida [--out PATH] [--keep-unique] PROFILE` | `./xenoid --instance phone-2 device generate-service-frida --help` |

Managed panel scripts also expose `Device.regenerate(timeout=0,
operation_key=key, post_cache=None)` through the device SDK. Bash aliases are
`device regenerate [TIMEOUT [OPERATION_KEY]]` and
`xenoid device regenerate [TIMEOUT [OPERATION_KEY]]`; use `0` before an explicit
key, for example `device regenerate 0 after-cleanup`. These SDK aliases use
durable receipts scoped to the current managed run and iteration. Retain the
same key across uncertain responses to resume or confirm the original operation;
omitting the key creates a fresh UUID for each call.

Python scripts can pass a durable `post_cache` plan containing `packages`,
`user` (`"current"` or a nonnegative integer), and `timeout` (`null`, `0`, or a
positive integer). The receipt records the plan before regeneration and each
cache-only success afterward. A retry resumes the same transaction and remaining
packages; changing the saved plan is rejected. See the [SDK example](../README.md#official-regeneration-from-a-managed-script).
Worker startup retries a transient pending receipt using the panel run interval
and the same saved operation/plan before another app iteration. Finite recovery
deadlines do not reset between attempts; stop, supersession and identity changes
remain authoritative.
`Device.clear_cache(package, user="current", timeout=None)` performs the same
official PackageManager cache-only operation independently, preserving app data.

The SDK waits under the native instance lock, preserves the same container,
instance ID, resource tag and storage, and verifies native reload, profile and
changed identities before reporting success. Safe official `up` preparation can
finish an earlier startup journal, but image/container/storage replacement and
seed operations are rejected. Native readiness checks remain active. Pending
operations must be resumed with the same key. Reapply script-selected display
settings after the native profile replay. The host CLI above keeps its official
`--dry-run` syntax; it does not use the SDK positional timeout/key arguments.
| `device keybox set FILE` | `./xenoid --instance phone-2 device keybox set --help` |
| `device keybox status` | `./xenoid --instance phone-2 device keybox status` |
| `device keybox clear` | `./xenoid --instance phone-2 device keybox clear --help` |
| `profile deploy-helper PATH [--remote-path PATH]` | `./xenoid --instance phone-2 profile deploy-helper --help` |
| `profile status` | `./xenoid --instance phone-2 profile status` |
| `profile env` | `./xenoid --instance phone-2 profile env` |
| `profile dump` | `./xenoid --instance phone-2 profile dump` |

Use a local JSON file in the pinned `dev.xenoid.fingerprint/v1` schema for your own QA devices. `device regenerate` rotates the selected instance's identities and restarts Android. `--dry-run` returns its plan. Use separate paths for profile reports, which can contain device identifiers.

```bash
sudo -H xenoid-fleet run 1 -- device collect
sudo -H xenoid-fleet run 2 -- device apply --instance-identity ./qa-profile.json
sudo -H xenoid-fleet run 2 -- device regenerate --dry-run
```

### Location, cellular, and network helper

| Syntax | Example |
| --- | --- |
| `location list` | `./xenoid location list` |
| `location status [--check]` | `./xenoid --instance phone-2 location status --check` |
| `location set COUNTRY` | `./xenoid --instance phone-2 location set --help` |
| `location apply [--default COUNTRY]` | `./xenoid --instance phone-2 location apply --help` |
| `cellular status` | `./xenoid --instance phone-2 cellular status` |
| `netctl deploy [PATH] [--remote-path PATH]` | `./xenoid --instance phone-2 netctl deploy --help` |
| `netctl status [--ifname IFNAME]` | `./xenoid --instance phone-2 netctl status --ifname rmnet_data0` |
| `netctl set-mac MAC [--ifname IFNAME]` | `./xenoid --instance phone-2 netctl set-mac --help` |

`location list` supplies valid country codes. `location apply` is used by `up`. Use the correct native helper for the guest's architecture when supplying a helper explicitly.

### Proxy configuration

| Syntax | Example |
| --- | --- |
| `proxy status [--check]` | `./xenoid --instance phone-2 proxy status --check` |
| `proxy set (--stdin \| --prompt \| --source-file FILE) [--kind KIND] [--name NAME] [--udp \| --no-udp] [--ipv6 \| --no-ipv6] [--allow-insecure-http] [--enable \| --no-enable]` | `./xenoid --instance phone-2 proxy set --source-file ./proxy.conf --no-enable` |
| `proxy subscribe (--stdin \| --prompt \| --source-file FILE) [--name NAME] [--udp \| --no-udp] [--ipv6 \| --no-ipv6] [--allow-insecure-http] [--enable \| --no-enable]` | `./xenoid --instance phone-2 proxy subscribe --source-file ./subscription.txt --no-enable` |
| `proxy import [--kind KIND] [--name NAME] [--udp \| --no-udp] [--ipv6 \| --no-ipv6] [--allow-insecure-http] [--enable \| --no-enable] FILE` | `./xenoid --instance phone-2 proxy import --no-enable ./proxy.conf` |
| `proxy on` | `./xenoid --instance phone-2 proxy on` |
| `proxy off` | `./xenoid --instance phone-2 proxy off` |
| `proxy clear [--discard-unreadable-state]` | `./xenoid --instance phone-2 proxy clear` |
| `proxy list` | `./xenoid --instance phone-2 proxy list` |
| `proxy select NAME` | `./xenoid --instance phone-2 proxy select 'QA lab'` |
| `proxy export --out FILE` | `./xenoid --instance phone-2 proxy export --out ./phone-2-proxy.conf` |
| `proxy prepare [--asset FILE]` | `./xenoid --instance phone-2 proxy prepare` |
| `proxy reconcile` | `./xenoid --instance phone-2 proxy reconcile` |

`KIND` accepts `endpoint`, `uri_list`, `clash`, or `subscription`. Input files must be regular, non-symlink files owned by the command's user with mode `0600`. `proxy list` supplies node names and `proxy reconcile` reapplies the saved desired state. Proxy configuration is instance-specific.

### Camera, apps, and input

| Syntax | Example |
| --- | --- |
| `camera status [--check]` | `./xenoid --instance phone-2 camera status --check` |
| `camera set {photo,video} FILE` | `./xenoid --instance phone-2 camera set photo ./qa-camera.png` |
| `camera mode {naturalized,faithful}` | `./xenoid --instance phone-2 camera mode faithful` |
| `camera clear {photo,video,all}` | `./xenoid --instance phone-2 camera clear photo` |
| `camera apply` | `./xenoid --instance phone-2 camera apply` |
| `app install PATH` | `./xenoid --instance phone-2 app install ./qa.apk` |
| `app uninstall PACKAGE` | `./xenoid --instance phone-2 app uninstall com.example.qa` |
| `app launch COMPONENT` | `./xenoid --instance phone-2 app launch com.example.qa/.MainActivity` |
| `input deploy PATH [--remote-path PATH]` | `./xenoid --instance phone-2 input deploy --help` |
| `input tap X Y` | `./xenoid --instance phone-2 input tap 540 1800` |
| `input swipe X1 Y1 X2 Y2 [DURATION_MS]` | `./xenoid --instance phone-2 input swipe 540 1600 540 400 500` |
| `automation plan SCRIPT` | `./xenoid --instance phone-2 automation plan examples/automation/tap-home.js` |
| `automation run-host SCRIPT [--execute]` | `./xenoid --instance phone-2 automation run-host examples/automation/tap-home.js` |
| `automation run SCRIPT` | `./xenoid --instance phone-2 automation run examples/automation/tap-home.js` |

`input swipe` defaults to 300 milliseconds. `automation plan` reads recognizable calls without evaluating JavaScript. `run-host` requires Node.js. `--execute` sends the planned actions to the authenticated daemon.

Managed panel scripts also provide `Device.input_status(timeout=None)`. This
reads the official native driver's readiness, Android input mapping and
foreground context without repairing anything or injecting an event. The
default uses the panel command timeout; `0` waits without a deadline. Official
SDK taps validate that mapping and, when needed, reannounce the same verified
event inode before sending the requested native touch. They do not switch input
backends or replace runtime artifacts.
Managed Bash scripts can read the same status with `device input-status
[TIMEOUT]`.
Foreground reads use the current selected-display WindowManager state (display 0
by default), excluding historical `LAST ANR` content. Duplicate or ambiguous current windows still fail readiness;
focus is checked again before official native input.

### Root helper, instrumentation, and internal helpers

| Syntax | Example |
| --- | --- |
| `root status` | `./xenoid --instance phone-2 root status` |
| `root exec [COMMAND ...]` | `./xenoid --instance phone-2 root exec id` |
| `frida fetch [--version VERSION] [--arch ARCH] [--out-dir DIR]` | `./xenoid --instance phone-2 frida fetch --arch android-arm64 --out-dir ./qa-frida` |
| `frida install [--version VERSION] [--arch ARCH] [--out-dir DIR] [--remote-path PATH]` | `./xenoid --instance phone-2 frida install --help` |
| `frida deploy PATH [--remote-path PATH]` | `./xenoid --instance phone-2 frida deploy --help` |
| `frida deploy-scripts [--scripts-dir DIR] [--remote-dir DIR]` | `./xenoid --instance phone-2 frida deploy-scripts --help` |
| `frida load-script [--spawn] [--oneshot] PACKAGE SCRIPT` | `./xenoid --instance phone-2 frida load-script --help` |
| `frida start [--port PORT]` | `./xenoid --instance phone-2 frida start --help` |
| `frida stop` | `./xenoid --instance phone-2 frida stop` |
| `frida status` | `./xenoid --instance phone-2 frida status` |
| `hide deploy PATH [--remote-path PATH]` | `./xenoid --instance phone-2 hide deploy --help` |
| `hide status` | `./xenoid --instance phone-2 hide status` |
| `hide apply [POLICY]` | `./xenoid --instance phone-2 hide apply --help` |
| `hide deploy-overlay [PATH] [--remote-path PATH]` | `./xenoid --instance phone-2 hide deploy-overlay --help` |
| `hide overlay-status` | `./xenoid --instance phone-2 hide overlay-status` |
| `hide cleanup-overlay` | `./xenoid --instance phone-2 hide cleanup-overlay --help` |

Native helper setup can recover a recordless current-version process only after
exact binary, process, instance-listener and current-protocol authentication
checks, followed by ownership revalidation and normal helper provisioning.
Unverified or foreign helpers remain rejected. Failed record/socket reads are
unknown state, never proof that a helper is absent.
When the Android daemon is unavailable, current owned helpers may authenticate
through the official direct root protocol. This never overrides authorization
failures. Verified identical deployed binaries can use normal startup without
replacing their inode through an unnecessary ADB upload.

`root exec` sends a command to the Android root helper for the selected instance. Frida's fetch defaults are the latest release and `android-arm64`. Its default listen port is `27042`. After an instrumentation session, stop instrumentation and restart the complete runtime before production acceptance.

### OTA and MCP entrypoints

| Syntax | Example |
| --- | --- |
| `ota make [--version VERSION]` | `./xenoid --instance phone-2 ota make --version 0.1.0` |
| `ota install-bundle BUNDLE` | `./xenoid --instance phone-2 ota install-bundle ./qa-ota-bundle.zip` |
| `ota check` | `./xenoid --instance phone-2 ota check` |
| `ota apply [--channel CHANNEL]` | `./xenoid --instance phone-2 ota apply --channel stable` |
| `mcp-config` | `./xenoid --instance phone-2 mcp-config` |

The pinned project provides separate executables `xenoid-mcp` and `xenoid-service` in the checkout. `xenoid-mcp` is a stdio process that uses the instance environment. The service's `--project PROJECT` goes before its subcommand. [MCP tool inventory](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/docs/mcp-tools.md) · [Service transport and access](https://github.com/Umiade/xenoid/blob/c07e56b4aef8cd7e4d56eed11c84781369d29997/docs/remote-service.md)

| Syntax | Example |
| --- | --- |
| `./xenoid-mcp` | `XENOID_INSTANCE=phone-2 ./xenoid-mcp` |
| `./xenoid-service [--project PROJECT] token create --name NAME (--all-instances \| --instance INSTANCE ...) [--scope {control,inspect,read,root} ...]` | `./xenoid-service token create --name qa-reader --instance phone-2 --scope read` |
| `./xenoid-service [--project PROJECT] token list` | `./xenoid-service token list` |
| `./xenoid-service [--project PROJECT] token revoke NAME` | `./xenoid-service token revoke qa-reader` |
| `./xenoid-service [--project PROJECT] serve [--bind ADDRESS] [--port PORT] [--allow-host HOST ...] [--allow-origin ORIGIN ...] [--tls-cert CERT] [--tls-key KEY] [--allow-insecure-http] [--max-request-bytes BYTES] [--request-timeout SECONDS] [--max-concurrency COUNT] [--requests-per-minute COUNT] [--verbose]` | `./xenoid-service serve --bind 127.0.0.1 --port 8765` |
