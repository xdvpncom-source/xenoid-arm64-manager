#!/usr/bin/env bash
set -euo pipefail

release=v0.1.35
fleet_repository=https://github.com/xdvpncom-source/xenoid-arm64-manager.git
fleet_sha256=ecf92ff661cfe7a20b8628f3508d86d6398b5ccd046fd8929db7ecfedc9692a3
toolchain_sha256=5b2c87c997d2e23ec19bdd950e8095a32e6a05f912be49d69fdcbad774aa5bf6
loader_layout_sha256=033ccfaf40120a40bab8a3daceb6d0b654e01efede326b01ac65f92b69b4bc4f
daemon_patch_sha256=a76a728fea8ebee104fc5e7230eb02ec69d11db5d24f51eff0f9edfa4f9ae467
keymint_patch_sha256=5fd4a5fe25479ebe8fa068bd8cf55fe72ddf3245787e19feaf4a06f67859fa16
protection_patch_sha256=1e57b90e7008a26a7f4a04a926ddcc82e9b79d78c367bba3562e41f4a19c26a3
proxy_acceptance_patch_sha256=680d7186162c9e48d5e27d251b4a1739337ffc2f3affcb1579ebf7a7339ef716
proxy_routing_patch_sha256=8bcebe3579457fb41637200710e73526aced59031bf0f5ef892c946f2236bc98
proxy_cleanup_patch_sha256=aefaabfef44122d850a4e273871835f52b5385fcb59a3827e28e8c86b7306aea
host_vm_patch_sha256=5671789f1561944b2952eadf132f006d18780f22ed89b2b51f15fd56db499556
qa_manifest_sha256=f42072aec5118edd6d192915578521023672f6b0cf252164914ba2b50e67b264
qa_installer_sha256=383563272585e48e19cccd7e00e309f817614f201c73255f0948b27f013cc7c2
proxy_upgrader_sha256=9994aab4e2aa074a35aac44c2a3da9e11d9aa320d89aba8c4fcace6639a3c444
native_manifest_sha256=2e1fb47f0f94441094fab7f7d162fec088ceb5ff74d82868d0ab28e18480d078
native_applier_sha256=256b74a271241b407425d8e44547c363881d13125709dc4ef05e7cb2caa8e305
native_artifacts_sha256=3ec779fa7c96a3fcb65dcde685884d310b4849c1a3913c4aea6da20c3e99954b
native_artifact_installer_sha256=f22c34f69f2c79539139156d452acdeea4857195961d0484ddd906b1f72bb380
fresh_config_sha256=fd30074046910e54d7d24acbdedb37e67043b425a7c94a7925d16e374ee96bb0
control_adapter_sha256=3ef8f472d1358e82c2e7b9a12487831971546e314fc0f49c161e75ef1d0ca0ac
upstream_repository=https://github.com/Umiade/xenoid.git
upstream_revision=c07e56b4aef8cd7e4d56eed11c84781369d29997
base_repository=redroid/redroid
base_tag=$base_repository:13.0.0_64only-latest
base_digest=sha256:5a42a569ee1d7c71796c0385e906cbaa4c3e0a162a56d9f26b29bdb1befac13b
prefix=/opt/xenoid-fleet
launcher=/usr/local/bin/xenoid-fleet
install_only=0
accept_licenses=0

usage() {
    cat <<'EOF'
Usage: sudo -H bash install.sh [--accept-android-licenses] [--install-only]

Install the Xenoid ARM64 fleet CLI on Ubuntu 24.04 and create the recommended
number of fresh Pixel 6a devices, reserving host CPU and RAM.
  --install-only             Prepare the host without starting any instance.
  --accept-android-licenses  Accept the Android SDK licenses for unattended installation.
EOF
}

fail() { printf 'xenoid-fleet installer: %s\n' "$*" >&2; exit 1; }

for argument in "$@"; do
    case "$argument" in
        --install-only) install_only=1 ;;
        --accept-android-licenses) accept_licenses=1 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option $argument. See --help." ;;
    esac
done

[[ $(uname -s) == Linux && $(uname -m) == aarch64 ]] || fail 'Use a native ARM64 Linux server.'
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || fail 'Use Ubuntu 24.04 LTS.'
[[ $EUID == 0 ]] || fail 'Run the installer with sudo -H.'
[[ ${HOME:-} == /root ]] || fail 'Use sudo -H so installation and later sudo commands use the same registry.'
[[ $(ps -p 1 -o comm=) == systemd ]] || fail 'This installer requires a systemd host.'
[[ -z ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} ]] || fail 'Clear DOCKER_HOST and DOCKER_CONTEXT to use the local engine.'

umask 022
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
install -d -o root -g root -m 0755 /opt
[[ ! -L $prefix ]] || fail "$prefix must be a real directory."
if [[ -e $prefix ]]; then
    [[ -d $prefix && $(stat -c '%u:%g:%a' "$prefix") == 0:0:755 ]] || fail "Check the ownership and mode of $prefix."
else
    install -d -o root -g root -m 0755 "$prefix"
fi
exec 9>"$prefix/.install.lock"
flock 9
stage=$(mktemp -d "$prefix/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

# Earlier Fleet toolchains created /lib64 as a real directory on ARM64.
# Repair only that recognized single-loader layout before APT can run a
# systemd/udev preinst. Standard usr-merged hosts require no bootstrap action.
if [[ -e /lib64 || -L /lib64 ]]; then
    if [[ -L /lib64 ]]; then
        [[ $(readlink -f -- /lib64) == /usr/lib* ]] || fail 'Unsupported /lib64 link outside /usr/lib.'
    else
        command -v python3 >/dev/null || fail 'The legacy /lib64 repair requires the previously installed Python 3.'
        loader_helper=$stage/repair-merged-lib64.py
        local_source=
        if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]:-} ]]; then
            local_source=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
        fi
        if [[ -n $local_source && -f $local_source/scripts/repair-merged-lib64.py ]]; then
            install -m 0600 "$local_source/scripts/repair-merged-lib64.py" "$loader_helper"
        else
            curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
                "https://raw.githubusercontent.com/xdvpncom-source/xenoid-arm64-manager/$release/scripts/repair-merged-lib64.py" \
                -o "$loader_helper"
        fi
        printf '%s  %s\n' "$loader_layout_sha256" "$loader_helper" | sha256sum --check --status || fail 'Loader layout repair helper digest changed.'
        python3 "$loader_helper" --execute || fail 'The existing /lib64 layout was not the recognized Fleet loader directory.'
    fi
fi

package_installed() { [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null || true) == 'ii ' ]]; }

apt_ready=0
install_packages() {
    local name audit
    local missing=()
    for name in "$@"; do
        package_installed "$name" || missing+=("$name")
    done
    audit=$(dpkg --audit)
    if (( ${#missing[@]} )) || [[ -n $audit ]]; then
        if (( ! apt_ready )); then apt-get update; apt_ready=1; fi
        apt-get install --fix-broken --no-remove -y --no-install-recommends ${missing[@]+"${missing[@]}"}
        audit=$(dpkg --audit)
        [[ -z $audit ]] || fail 'Package configuration remains incomplete after dependency repair.'
    fi
}

printf 'Preparing Ubuntu build dependencies\n'
install_packages ca-certificates curl git gnupg python3 sudo kmod adb e2fsprogs xz-utils \
    build-essential clang llvm libbpf-dev libelf-dev zlib1g-dev libssl-dev pkg-config \
    cmake ninja-build openjdk-17-jdk-headless unzip zip file rsync iptables nodejs
kernel=$(uname -r)
[[ -d /lib/modules/$kernel/build ]] || install_packages "linux-headers-$kernel"
if ! command -v bpftool >/dev/null || ! bpftool version >/dev/null 2>&1; then
    install_packages "linux-tools-$kernel"
fi
bpftool version >/dev/null || fail "bpftool is unavailable for the running kernel $kernel."
[[ -r /sys/kernel/btf/vmlinux ]] || fail "Kernel $kernel does not expose vmlinux BTF."
if ! modinfo binder_linux >/dev/null 2>&1 && [[ ! -d /sys/module/binder_linux ]]; then
    install_packages "linux-modules-extra-$kernel"
fi

printf 'Loading the kernel netfilter tables required by Xenoid\n'
for module in ip_tables ip6_tables; do
    if ! modinfo "$module" >/dev/null 2>&1 && [[ ! -d /sys/module/$module ]]; then
        install_packages "linux-modules-extra-$kernel"
    fi
done
modprobe -a ip_tables ip6_tables
[[ -r /proc/net/ip_tables_targets && -r /proc/net/ip6_tables_targets ]] || \
    fail 'Xenoid requires IPv4 and IPv6 kernel netfilter tables.'
printf '%s\n' ip_tables ip6_tables > "$stage/xenoid-fleet-netfilter.conf"
install -m 0644 "$stage/xenoid-fleet-netfilter.conf" /etc/modules-load.d/xenoid-fleet-netfilter.conf

printf 'Checking the local Docker engine\n'
if ! command -v docker >/dev/null; then
    for name in docker.io docker-compose-v2 podman-docker containerd runc; do
        if package_installed "$name"; then
            fail "Existing package $name conflicts with Docker CE. Reuse a working Docker Engine with Buildx or prepare a fresh host."
        fi
    done
    install -d -m 0755 /etc/apt/keyrings
    if [[ ! -e /etc/apt/sources.list.d/docker.sources && ! -e /etc/apt/sources.list.d/docker.list ]]; then
        curl --proto '=https' --tlsv1.2 -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o "$stage/docker.asc"
        fingerprint=$(gpg --homedir "$stage" --batch --show-keys --with-colons "$stage/docker.asc" | \
            awk -F: '$1=="pub" {primary=1} $1=="fpr" && primary {print $10; primary=0}')
        [[ $fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || fail 'Docker signing key fingerprint did not match.'
        install -m 0644 "$stage/docker.asc" /etc/apt/keyrings/xenoid-fleet-docker.asc
        cat > "$stage/docker.sources" <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/xenoid-fleet-docker.asc
EOF
        install -m 0644 "$stage/docker.sources" /etc/apt/sources.list.d/docker.sources
    fi
    apt_ready=0
    install_packages docker-ce docker-ce-cli containerd.io docker-buildx-plugin
elif ! docker buildx version >/dev/null 2>&1 && package_installed docker-ce; then
    install_packages docker-buildx-plugin
fi
docker buildx version >/dev/null || fail 'Docker Buildx is required. Install the Buildx plugin for the existing engine.'
endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
[[ $endpoint == unix:///var/run/docker.sock || $endpoint == unix:///run/docker.sock ]] || fail 'Select the local system Docker context.'
systemctl enable --now docker.service
[[ $(docker info --format '{{.Architecture}}') == aarch64 ]] || fail 'Docker must report the local ARM64 engine.'
[[ $(docker info --format '{{json .DriverStatus}}') == *io.containerd.snapshotter.v1* ]] || \
    fail 'Xenoid requires Docker’s containerd image store for the pinned Android base. Check the existing engine configuration.'

source_tree=
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]:-} ]]; then
    source_tree=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ -z $source_tree || ! -f $source_tree/xenoid-fleet || ! -f $source_tree/install-toolchain.sh ]]; then
    git init -q "$stage/fleet"
    git -C "$stage/fleet" fetch -q --depth=1 "$fleet_repository" "refs/tags/$release"
    git -C "$stage/fleet" checkout -q --detach 'FETCH_HEAD^{commit}'
    source_tree=$stage/fleet
fi
(
    cd "$source_tree"
    printf '%s  %s\n' "$fleet_sha256" xenoid-fleet "$toolchain_sha256" install-toolchain.sh \
        "$loader_layout_sha256" scripts/repair-merged-lib64.py \
        "$daemon_patch_sha256" patches/standalone-manifest.patch "$keymint_patch_sha256" patches/keymint-paths.patch \
        "$protection_patch_sha256" patches/shared-protection-inspect.patch \
        "$proxy_acceptance_patch_sha256" patches/proxy-live-acceptance.patch \
        "$proxy_routing_patch_sha256" patches/proxy-domain-routing.patch \
        "$proxy_cleanup_patch_sha256" patches/proxy-namespaced-cleanup.patch \
        "$host_vm_patch_sha256" patches/host-vm-isolation.patch \
        "$qa_manifest_sha256" qa/manifest.sha256 "$qa_installer_sha256" install-qa-addon.sh \
        "$proxy_upgrader_sha256" scripts/upgrade-proxy-control.py \
        "$native_manifest_sha256" nativebundle/manifest.json \
        "$native_applier_sha256" nativebundle/apply.py \
        "$native_artifacts_sha256" nativebundle/artifacts.json \
        "$native_artifact_installer_sha256" nativebundle/install_artifacts.py \
        "$fresh_config_sha256" defaults/bluejay-linux-arm.json \
        "$control_adapter_sha256" control/panel_device_create.py | \
        sha256sum --check --status
) || fail "The CLI, toolchain installer, or source patches did not match release $release."

printf 'Preparing the pinned Xenoid source\n'
upstream=$prefix/upstream
if [[ -e $upstream || -L $upstream ]]; then
    [[ -d $upstream/.git && ! -L $upstream && $(git -C "$upstream" rev-parse HEAD) == "$upstream_revision" ]] || \
        fail "The checkout at $upstream differs from the pinned revision."
else
    git init -q "$stage/upstream"
    git -C "$stage/upstream" fetch -q --depth=1 "$upstream_repository" "$upstream_revision"
    [[ $(git -C "$stage/upstream" rev-parse FETCH_HEAD) == "$upstream_revision" ]] || fail 'Xenoid revision verification failed.'
    git -C "$stage/upstream" checkout -q --detach FETCH_HEAD
    mv "$stage/upstream" "$upstream"
fi


printf 'Publishing the verified native source bundle\n'
python3 "$source_tree/nativebundle/apply.py" --upstream "$upstream" \
    --bundle "$source_tree/nativebundle" --manifest-sha256 "$native_manifest_sha256"

# The native artifact producer independently validates this archive's source,
# ABI, CFI, hashes and provenance. No Android container is started here.
python3 - "$source_tree/nativebundle/artifacts.json" > "$stage/native-artifacts.txt" <<'PYART'
import json,re,sys
v=json.load(open(sys.argv[1]))
assert set(v)=={"schema","file","sha256","bytes","google_payload_included","license_files","public_provenance","service_sha256","source_patch_sha256"}
assert v["google_payload_included"] is False and v["public_provenance"] is True
assert type(v["license_files"]) is int and v["license_files"]>0
assert all(isinstance(v[key],str) and re.fullmatch("[0-9a-f]{64}",v[key]) for key in ["service_sha256","source_patch_sha256"])
assert v["schema"]=="dev.xenoid.fleet.native-artifacts/v1"
assert isinstance(v["file"],str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.xz",v["file"])
assert isinstance(v["sha256"],str) and re.fullmatch("[0-9a-f]{64}",v["sha256"])
assert type(v["bytes"]) is int and v["bytes"]>0
print(v["file"]);print(v["sha256"]);print(v["bytes"])
PYART
mapfile -t artifact_spec < "$stage/native-artifacts.txt"
[[ ${#artifact_spec[@]} == 3 ]] || fail 'The native artifact descriptor is invalid.'
artifact_file=${artifact_spec[0]}
artifact_sha=${artifact_spec[1]}
artifact_bytes=${artifact_spec[2]}
if [[ -f "$source_tree/nativebundle/$artifact_file" && ! -L "$source_tree/nativebundle/$artifact_file" ]]; then
    install -m 0600 "$source_tree/nativebundle/$artifact_file" "$stage/$artifact_file"
else
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
        "https://github.com/xdvpncom-source/xenoid-arm64-manager/releases/download/$release/$artifact_file" \
        -o "$stage/$artifact_file"
fi
[[ $(stat -c %s "$stage/$artifact_file") == "$artifact_bytes" ]] || fail 'The native artifact size differs from the release descriptor.'
printf '%s  %s\n' "$artifact_sha" "$stage/$artifact_file" | sha256sum --check --status || \
    fail 'The native artifact digest differs from the release descriptor.'
python3 "$source_tree/nativebundle/install_artifacts.py" --archive "$stage/$artifact_file" \
    --upstream "$upstream" --bundle "$source_tree/nativebundle" \
    --manifest-sha256 "$native_manifest_sha256" --artifacts-sha256 "$native_artifacts_sha256"

printf 'Checking binderfs and the ARM64 Android base\n'
bash "$upstream/scripts/setup-linux-binderfs.sh"
if [[ $(docker image inspect "$base_tag" --format '{{.Id}} {{.Architecture}}' 2>/dev/null || true) != "$base_digest arm64" ]]; then
    docker pull --platform linux/arm64 "$base_repository@$base_digest"
    [[ $(docker image inspect "$base_repository@$base_digest" --format '{{.Id}} {{.Architecture}}') == "$base_digest arm64" ]] || \
        fail 'The Android base did not match the pinned ARM64 image identity.'
    docker tag "$base_repository@$base_digest" "$base_tag"
fi

printf 'Preparing the Android build toolchain\n'
XENOID_ACCEPT_ANDROID_SDK_LICENSES=$accept_licenses bash "$source_tree/install-toolchain.sh" "$prefix/toolchain"
install -m 0755 "$source_tree/xenoid-fleet" "$stage/xenoid-fleet"
install -m 0755 "$source_tree/scripts/repair-merged-lib64.py" "$prefix/repair-merged-lib64.py"
mv -f -- "$stage/xenoid-fleet" "$prefix/xenoid-fleet"
install -d -m 0755 /usr/local/bin
if [[ -L $launcher && $(readlink "$launcher") == "$prefix/xenoid-fleet" ]]; then
    :
elif [[ -e $launcher || -L $launcher ]]; then
    fail "$launcher is already occupied."
else
    ln -s "$prefix/xenoid-fleet" "$launcher"
fi

install -d -o root -g root -m 0755 "$prefix/defaults"
install -o root -g root -m 0644 "$source_tree/defaults/bluejay-linux-arm.json" "$prefix/defaults/bluejay-linux-arm.json"
install -d -o root -g root -m 0755 "$prefix/control"
install -o root -g root -m 0644 "$source_tree/control/panel_device_create.py" "$prefix/control/panel_device_create.py"

printf 'Installing panel-controlled QA helpers and host resource services\n'
bash "$source_tree/install-qa-addon.sh"
install -m 0755 "$source_tree/scripts/upgrade-proxy-control.py" "$stage/upgrade-proxy-control.py"
mv -f -- "$stage/upgrade-proxy-control.py" "$prefix/upgrade-proxy-control.py"
printf 'Refreshing enabled running proxies for the verified native control revision\n'
python3 "$prefix/upgrade-proxy-control.py" --all

if (( ! install_only )); then
    unset XENOID_EXPECT_INSTANCE_ID XENOID_INSTANCE XENOID_FLEET_UPSTREAM
    "$launcher" capacity --json > "$stage/capacity.json"
    python3 - "$stage/capacity.json" > "$stage/device-selector" <<'PYCAP'
import json,sys
v=json.load(open(sys.argv[1]))
n=v.get("recommended_count")
assert v.get("schema")=="dev.xenoid.fleet.capacity/v1" and v.get("ok") is True
assert type(n) is int and n>=0
assert v.get("swap_capacity_ok") is True or n==0
print(",".join(str(i)for i in range(1,n+1)))
PYCAP
    selector=$(cat "$stage/device-selector")
    if [[ -n $selector ]]; then
        "$launcher" create "$selector" --if-missing
    else
        printf 'Host reserves leave no automatic device capacity; no Android instance was created.\n'
    fi
fi
printf 'Installed %s. List devices with sudo -H xenoid-fleet list.\n' "$launcher"
