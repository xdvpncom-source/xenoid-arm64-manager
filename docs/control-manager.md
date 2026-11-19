# Fleet control manager

`fleet-control` runs on the machine that hosts the management panel. The controller stores inventory, credentials, and jobs locally and runs Xenoid commands on registered hosts over SSH. The web application invokes the same CLI. The controller serves CLI commands and jobs independently of the web application. Use [the Xenoid and Fleet command reference](commands.md) for device commands forwarded through `device run`.

On the manager host, `sudo fleet-control --help` opens the CLI help. `--json` selects structured output and `--socket PATH` selects a different local controller socket. Place these global flags before the subcommand. For an alternate socket, use `sudo fleet-control --socket /run/example/control.sock status`.

## Server registration

| Command | Example | Effect |
| --- | --- | --- |
| `status` | `sudo fleet-control --json status` | Read controller and queue status. |
| `server list` | `sudo fleet-control --json server list` | Read the registered SSH inventory. |
| `server add HOST` | `sudo fleet-control server add qa-arm64.example.com --name qa-node --username root --port 22 --device-count 4 --wait` | Prompt for a password, check via SSH, install Fleet if needed, and ensure IDs 1 through 4. |
| `server add HOST --identity-file PATH` | `sudo fleet-control server add qa-arm64.example.com --identity-file ./manager-key --device-count 4` | Register with an SSH private key. Add `--host-key-fingerprint SHA256:…` to pin a verified server key. |
| `server edit ID` | `sudo fleet-control server edit 1 --name qa-node --change-password` | Edit the record and prompt for a new password. Use `--identity-file PATH` to change to key authentication. |
| `server check` | `sudo fleet-control server check --server 1,2 --wait` | Check connectivity, installation, and device status. |
| `server install` | `sudo fleet-control server install --server 1 --device-count 4 --wait` | Install the published package where needed and create missing IDs. |
| `server remove ID` | `sudo fleet-control server remove 1` | Remove the record from the manager inventory. |

`server add` and `server edit` also accept `--identity-stdin` for a key or `--password-stdin` for one password line. Read the credential from standard input or use the interactive prompt. `--prompt-key-passphrase` reads the key passphrase interactively. `server edit` accepts `--hostname HOST`, `--name`, `--username`, `--port`, and `--host-key-fingerprint`. The controller requires root or passwordless sudo on key-authenticated managed hosts.

## Targets and devices

`--server 1`, `--server 1,2`, and `--server all` select manager inventory IDs. A `--devices` filter identifies the numeric Xenoid devices on each selected server. `--wait` follows a queued action through its exit status.

| Command | Example |
| --- | --- |
| `device list` | `sudo fleet-control --json device list --server all` |
| `device refresh` | `sudo fleet-control device refresh --server all --wait` |
| `device create` with IDs | `sudo fleet-control device create --server 1 --devices 2,3 --wait` |
| `device create` to a count | `sudo fleet-control device create --server 1 --count 4 --wait` |
| `device create` with JSON | `sudo fleet-control device create --server 1 --devices 2 --profile /srv/profiles/qa.json --wait` |
| `device start` | `sudo fleet-control device start --server 1,2 --devices all --wait` |
| `device stop` | `sudo fleet-control device stop --server 1 --devices 2 --wait` |
| `device run` | `sudo fleet-control device run --server all --devices 1 --wait -- adb shell getprop ro.product.model` |
| `device delete` preview | `sudo fleet-control device delete --server 1 --devices 3 --dry-run --wait` |
| `device delete` confirmed | `sudo fleet-control device delete --server 1 --devices 3 --yes --wait` |
| `device screenshot HOST_ID DEVICE_ID` | `sudo fleet-control device screenshot 1 2 --output ./phone-2.png` |
| `device live HOST_ID DEVICE_ID` | `sudo fleet-control device live 1 2 --frames 30 --interval 2.5 --output ./phone-2-frames` |

`device list` reads the controller's cached inventory. `device refresh` updates it via the remote CLI. `--count N` ensures IDs 1 through N and preserves IDs that are already registered. Explicit IDs follow the native `xenoid-fleet create` recreate behavior. Use a profile path on the managed server. Use `--output -` for a screenshot on standard output. Live view takes successive PNG frames, with 1 to 3600 frames and 1 to 60 seconds between captures. The web panel offers live frame grids for multiple devices.

## Xenoid proxy

The panel and controller use the published Fleet launcher to run Xenoid's native proxy commands per device. Supply a UTF-8 text source up to 64 KiB through `--source-file PATH` or `--stdin`. The controller forwards it through SSH and stages a private file for the native command. Job logs show the operation and verified status fields.

| Command | Example |
| --- | --- |
| `proxy set` | `sudo fleet-control proxy set --server all --source-file ./proxy.txt --kind endpoint --no-udp --wait` |
| `proxy status` | `sudo fleet-control proxy status --server all --wait` |
| `proxy off` | `sudo fleet-control proxy off --server all --wait` |
| `proxy on` | `sudo fleet-control proxy on --server 2 --devices 3 --wait` |

An endpoint source can contain `socks5://USER:PASSWORD@HOST:1080` or `http://USER:PASSWORD@HOST:8080`. Percent-encode reserved characters in the username or password. Add `--no-udp` for HTTP and HTTPS proxy endpoints. Use `--kind subscription` for subscription URLs. Other source types are `uri_list` and `clash`. Use `--no-enable` to save a configuration in the disabled state and `--no-ipv6` when the proxy provider lacks IPv6. Select a node with `--name NAME` or explicitly allow an HTTP subscription through `--allow-insecure-http`.

## APK uploads and percentage installs

| Command | Example |
| --- | --- |
| `apk upload PATH` | `sudo fleet-control apk upload ./qa.apk` |
| `apk upload - --name NAME --size BYTES` | `sudo fleet-control apk upload - --name qa.apk --size "$(wc -c < qa.apk)" < qa.apk` |
| `apk list` | `sudo fleet-control --json apk list` |
| `apk deploy ID --percent N` | `sudo fleet-control apk deploy 1 --percent 35 --wait` |

The library accepts a single APK file up to 250 MiB per upload. Refresh the device inventory to include external registry changes before deploying. The controller rounds the percentage of registered devices to the nearest whole device, rounding a half up. The same APK selects the same device cohort at a given percentage while the inventory stays the same. Use 0% to save a zero-target selection. A later percentage sets the next install cohort. Removing an existing app copy is a separate operation.

The controller uploads and verifies one temporary copy per host in parallel, then runs `/usr/local/bin/xenoid-fleet run ID -- app install PATH` for each selected device. Installation jobs report the outcome for each device. Selected devices that are stopped are recorded as failures so they can be started and retried.

## Parallel jobs across servers

An action with `--server all` submits one job and dispatches each registered server independently. A server starts when a worker slot is available and its earlier submissions have finished. The production configuration sets `FLEET_CONTROL_WORKERS=22`, so up to 22 servers can run concurrently across all jobs. Other deployments default to 6 and accept values from 1 to 24. Actions on the same server run one at a time in submission order.

```sh
# Refresh all servers, then print the inventory from the manager.
sudo fleet-control device refresh --server all --wait
sudo fleet-control --json device list --server all

# Read the model of device 1 independently on each server.
sudo fleet-control device run --server all --devices 1 --wait -- adb shell getprop ro.product.model

# Install Fleet where needed and ensure device 1 exists on each server.
sudo fleet-control server install --server all --device-count 1 --wait
```

`--wait` follows the grouped job until all targets finish. A failure on one server leaves the other servers running and makes the final exit status nonzero. The transcript labels output by host ID, and `job show ID` includes `host_results` with each server's status, error, and timestamps. Use a selector such as `--server 2,5` to retry only the servers that need it.

## Jobs and saved SSH sequences

| Command | Example |
| --- | --- |
| `job list` | `sudo fleet-control job list` |
| `job show ID` | `sudo fleet-control job show 12` |
| `job wait ID` | `sudo fleet-control job wait 12` |
| `job cancel ID` | `sudo fleet-control job cancel 12` |
| `loop list` | `sudo fleet-control loop list` |
| `loop add NAME` | `sudo fleet-control loop add health --command 'uptime' --command 'df -h /' --repeat 3 --interval 15` |
| `loop run ID` | `sudo fleet-control loop run 1 --server 1,2 --wait` |
| `loop remove ID` | `sudo fleet-control loop remove 1` |
| `rpc` | `printf '%s\n' '{"method":"status","parameters":{}}' \| sudo fleet-control rpc` |

A loop runs its ordered lines in the same noninteractive SSH shell. `--repeat` accepts 1 to 10 rounds and `--interval` accepts 0 to 300 seconds between rounds. `--continue-on-error` lets later lines run after a command fails. An active cancellation skips subsequent work after the in-flight SSH command returns. `rpc` reads one JSON request from standard input. The local socket controls access to the manager.
