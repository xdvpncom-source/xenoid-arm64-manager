# Xenoid ARM64 Fleet

Install Xenoid on an ARM64 Ubuntu server and manage headless Android 13 instances through a local CLI. Each numeric ID maps to an upstream instance name. For example, `2` selects `phone-2`. Xenoid allocates the container, persistent volume, network, and loopback ADB port for each instance.

## Install

Use an **ARM64 (aarch64) server running Ubuntu 24.04**, such as a Hetzner CAX server. The installer requires root access, outbound HTTPS, and free disk space for the build toolchain, container images, and Android data. Install and management commands use the same operator account and Xenoid registry.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.6/install.sh | sudo -H bash -s -- --accept-android-licenses
```

The `--accept-android-licenses` flag accepts the Android SDK licenses for the pinned build tools from Google. The installer prepares the host and starts device `1`. Allow tens of minutes for the first build on a small server. The CLI is installed as `/usr/local/bin/xenoid-fleet` and the managed checkout lives under `/opt/xenoid-fleet/upstream`. Rerunning the installer reuses the toolchain and converges device `1` with its existing data.

Android runs natively on ARM64 under Docker. An existing Docker engine needs Buildx and the containerd image store, which is the default on new Docker Engine 29 installations. The installer enables x86-64 userspace emulation for Google's Linux SDK and NDK executables. The Xenoid build cache and Docker images stay on the server for subsequent devices. The fleet CLI gives image builds separate Buildx client state so Xenoid can verify their payloads against the current build inputs.

The installer loads the IPv4 and IPv6 kernel netfilter tables that Xenoid checks during startup. It verifies and applies patches to the pinned Xenoid source. The standalone daemon build supplies the same package and version metadata as Gradle and reads Java source as UTF-8. The KeyMint build remaps paths from the installed Rust toolchain before artifact validation. The shared runtime inventory inspects containers individually so large fleets fit within the command output limit while retaining the same ownership checks.

Check that Android booted. The command output includes the value `1` after boot completes.

```bash
sudo -H xenoid-fleet run 1 -- adb shell getprop sys.boot_completed
```

Use `--install-only` to prepare the host before creating the first device or upgrade an existing installation without issuing a device start.

```bash
curl -fsSL https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/v0.1.6/install.sh | sudo -H bash -s -- --install-only --accept-android-licenses
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

Place an independently signature-verified ARM64 Google Chrome APK bundle and its matching Trichrome Library privately at `/opt/xenoid-fleet/chrome-baseline`. Use owner and group `root:root`, mode `0700` for the directory and `0600` for every file referenced by `manifest.json` and the manifest itself. Keep the APKs and their manifest private. The Fleet release supplies the loader.

`manifest.json` uses schema `dev.xenoid.fleet.chrome-baseline/v1`, a numeric Chrome `version_code`, a `chrome` array of APK records in install order with `base.apk` first, and a `trichrome` APK record for `trichrome.apk`. Each record has a leaf `file` name and its lowercase `sha256` digest.

`xenoid-fleet create` verifies the bundle before creating or recreating a device. After startup it installs Trichrome through Xenoid `app install` and Chrome through the instance-scoped `adb install-multiple -r`, then checks the installed Chrome version. A plain `xenoid-fleet run ID -- up` also applies the baseline, including during installer startup. Existing equal or higher Chrome versions are preserved. `--install-only` limits the installer to host preparation.

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

After a server restart, use `sudo -H xenoid-fleet run all -- up` to start the registered devices and reuse their data.

If `up` reports `proxy_disabled_state_unverified` for a running device, reconcile its saved proxy policy and retry `up`. For example, these commands recover device `1`.

```bash
sudo -H xenoid-fleet run 1 -- proxy reconcile
sudo -H xenoid-fleet run 1 -- up
```

## راهنمای سریع فارسی

روی سرور Ubuntu 24.04 با معماری ARM64، دستور بخش Install را اجرا کنید. برای ساخت دستگاه شمارهٔ ۲ از `sudo -H xenoid-fleet create 2` و برای ساخت یا بازسازی همان دستگاه با پروفایل از `sudo -H xenoid-fleet create 2 ./qa-profile.json` استفاده کنید. `list` وضعیت دستگاه‌ها را نشان می‌دهد. برای اجرای یک فرمان روی چند دستگاه، IDها را با کاما جدا کنید. برای نمونه، `sudo -H xenoid-fleet run 2,3 -- status` وضعیت دستگاه‌های ۲ و ۳ را می‌دهد. دستور `delete` داده‌های اندروید دستگاه‌های انتخاب‌شده را پاک می‌کند. اول با `--dry-run` مقصدها را بررسی کنید.

## Upstream

This repository provides the host installer and fleet CLI. The upstream [Xenoid project](https://github.com/Umiade/xenoid) provides the Android runtime and per-instance commands.
