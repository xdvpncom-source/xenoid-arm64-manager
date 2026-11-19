#!/usr/bin/env bash
set -euo pipefail

release=v0.1.7
fleet_repository=https://github.com/xdvpncom-source/xenoid-arm64-manager.git
fleet_sha256=3ae2cc040d15d206c00028771f8ad75ace148cd6c83c66a6c4dcbf36d2c6ba4b
toolchain_sha256=f657aba4c546b5c6c3a9c37b513db9ceca4fb31f26b33bf3106a5852052cf9c8
daemon_patch_sha256=a76a728fea8ebee104fc5e7230eb02ec69d11db5d24f51eff0f9edfa4f9ae467
keymint_patch_sha256=5fd4a5fe25479ebe8fa068bd8cf55fe72ddf3245787e19feaf4a06f67859fa16
protection_patch_sha256=1e57b90e7008a26a7f4a04a926ddcc82e9b79d78c367bba3562e41f4a19c26a3
v011_daemon_blob=ef4394904d9798a529fb8cf609989cf8526b4a89
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

Install the Xenoid ARM64 fleet CLI on Ubuntu 24.04 and start device 1.
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

package_installed() { [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null || true) == 'ii ' ]]; }

apt_ready=0
install_packages() {
    local name
    local missing=()
    for name in "$@"; do
        package_installed "$name" || missing+=("$name")
    done
    if (( ${#missing[@]} )); then
        if (( ! apt_ready )); then apt-get update; apt_ready=1; fi
        apt-get install -y --no-install-recommends "${missing[@]}"
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

printf 'Preparing the pinned Xenoid source\n'
upstream=$prefix/upstream
if [[ -e $upstream || -L $upstream ]]; then
    [[ -d $upstream/.git && ! -L $upstream && $(git -C "$upstream" rev-parse HEAD) == "$upstream_revision" ]] || \
        fail "The checkout at $upstream differs from the pinned revision."
    # The stock build replaces its tracked proxy sandbox binary with a validated artifact.
    git -C "$upstream" diff --quiet HEAD -- . ':(exclude)native/xenoid-proxy-sandbox/xenoid-proxy-sandbox' \
        ':(exclude)scripts/build-daemon-standalone.sh' ':(exclude)scripts/build-keymint.sh' \
        ':(exclude)src/xenoid/protection.py' || \
        fail 'The installed Xenoid tracked sources have local changes.'
else
    git init -q "$stage/upstream"
    git -C "$stage/upstream" fetch -q --depth=1 "$upstream_repository" "$upstream_revision"
    [[ $(git -C "$stage/upstream" rev-parse FETCH_HEAD) == "$upstream_revision" ]] || fail 'Xenoid revision verification failed.'
    git -C "$stage/upstream" checkout -q --detach FETCH_HEAD
    mv "$stage/upstream" "$upstream"
fi

printf 'Checking binderfs and the ARM64 Android base\n'
bash "$upstream/scripts/setup-linux-binderfs.sh"
if [[ $(docker image inspect "$base_tag" --format '{{.Id}} {{.Architecture}}' 2>/dev/null || true) != "$base_digest arm64" ]]; then
    docker pull --platform linux/arm64 "$base_repository@$base_digest"
    [[ $(docker image inspect "$base_repository@$base_digest" --format '{{.Id}} {{.Architecture}}') == "$base_digest arm64" ]] || \
        fail 'The Android base did not match the pinned ARM64 image identity.'
    docker tag "$base_repository@$base_digest" "$base_tag"
fi

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
        "$daemon_patch_sha256" patches/standalone-manifest.patch "$keymint_patch_sha256" patches/keymint-paths.patch \
        "$protection_patch_sha256" patches/shared-protection-inspect.patch | \
        sha256sum --check --status
) || fail "The CLI, toolchain installer, or source patches did not match release $release."

printf 'Preparing the pinned Xenoid source patches\n'
patched_sources=(scripts/build-daemon-standalone.sh scripts/build-keymint.sh src/xenoid/protection.py)
mkdir "$stage/reference"
git -C "$upstream" archive HEAD -- "${patched_sources[@]}" | tar -x --no-same-permissions -C "$stage/reference"
git -C "$stage/reference" apply "$source_tree/patches/standalone-manifest.patch" "$source_tree/patches/keymint-paths.patch" \
    "$source_tree/patches/shared-protection-inspect.patch"
for source_file in "${patched_sources[@]}"; do
    installed_source=$upstream/$source_file
    expected_mode=755
    if [[ $source_file == src/xenoid/protection.py ]]; then expected_mode=644; fi
    [[ -f $installed_source && ! -L $installed_source &&
        $(stat -c '%u:%g:%a' "$installed_source") == "0:0:$expected_mode" ]] || \
        fail "Check the ownership and mode of $installed_source."
    if ! cmp -s "$installed_source" "$stage/reference/$source_file"; then
        installed_blob=$(git -C "$upstream" hash-object --no-filters "$installed_source")
        [[ $installed_blob == $(git -C "$upstream" rev-parse "HEAD:$source_file") ||
            ( $source_file == scripts/build-daemon-standalone.sh && $installed_blob == "$v011_daemon_blob" ) ]] || \
            fail "The installed Xenoid source $source_file has local changes."
    fi
done
for source_file in "${patched_sources[@]}"; do
    if ! cmp -s "$upstream/$source_file" "$stage/reference/$source_file"; then
        mv -f -- "$stage/reference/$source_file" "$upstream/$source_file"
    fi
done

printf 'Preparing the Android build toolchain\n'
XENOID_ACCEPT_ANDROID_SDK_LICENSES=$accept_licenses bash "$source_tree/install-toolchain.sh" "$prefix/toolchain"
install -m 0755 "$source_tree/xenoid-fleet" "$stage/xenoid-fleet"
mv -f -- "$stage/xenoid-fleet" "$prefix/xenoid-fleet"
install -d -m 0755 /usr/local/bin
if [[ -L $launcher && $(readlink "$launcher") == "$prefix/xenoid-fleet" ]]; then
    :
elif [[ -e $launcher || -L $launcher ]]; then
    fail "$launcher is already occupied."
else
    ln -s "$prefix/xenoid-fleet" "$launcher"
fi

if (( ! install_only )); then
    cd "$upstream"
    unset XENOID_EXPECT_INSTANCE_ID XENOID_INSTANCE XENOID_FLEET_UPSTREAM
    registry=$(XENOID_PROJECT=$upstream "$upstream/xenoid" instance list)
    device_one=$(python3 -c 'import json, sys; rows = json.load(sys.stdin)["instances"]; print("yes" if any(r["instanceName"] == "phone-1" for r in rows) else "no")' <<< "$registry")
    if [[ $device_one == yes ]]; then
        initial_command=("$launcher" run 1 -- up)
    else
        initial_command=("$launcher" create 1)
    fi
    if "${initial_command[@]}"; then
        :
    else
        code=$?
        [[ $code == 1 ]] || exit "$code"
        printf 'Initial startup failed. Retrying once through the Xenoid registry.\n' >&2
        "$launcher" run 1 -- up
    fi
fi
printf 'Installed %s. List devices with sudo -H xenoid-fleet list.\n' "$launcher"
