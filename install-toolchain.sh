#!/usr/bin/env bash
# Install the Linux host tools needed to build the unmodified Xenoid runtime.
set -euo pipefail
umask 022

readonly sdk_build_tools=35.0.0
readonly sdk_platform=android-35
readonly sdk_ndk=27.2.12479018
readonly sdk_cmake=3.22.1
# Android Developers publishes the checksum for this exact Linux archive.
readonly sdk_cli_archive=commandlinetools-linux-15859902_latest.zip
readonly sdk_cli_sha=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583
readonly rustup_archive_url=https://static.rust-lang.org/rustup/archive/1.29.0/aarch64-unknown-linux-gnu/rustup-init
readonly rustup_archive_sha=9732d6c5e2a098d3521fca8145d826ae0aaa067ef2385ead08e6feac88fa5792
readonly cargo_ndk_release=4.1.2
readonly amd64_library_dir=/usr/x86_64-linux-gnu/lib

die() { printf 'xenoid toolchain: %s\n' "$*" >&2; exit 1; }
note() { printf 'xenoid toolchain: %s\n' "$*" >&2; }

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
  printf '%s\n' 'Usage: install-toolchain.sh /absolute/toolchain-prefix' \
    'Set XENOID_ACCEPT_ANDROID_SDK_LICENSES=1 to accept the Android SDK licenses.' \
    'Set XENOID_ANDROID_SDK_ROOT to validate and reuse an existing SDK.' \
    'Set XENOID_REUSE_RUSTUP=1 to validate and reuse an existing Rust toolchain.'
  exit 0
fi
[[ $# -eq 1 && $1 =~ ^/[a-zA-Z0-9._/-]+$ ]] || die 'pass an absolute toolchain directory'
[[ $(id -u) == 0 ]] || die 'run as root'
[[ $(uname -s) == Linux && $(dpkg --print-architecture) == arm64 ]] || die 'requires Ubuntu ARM64'
# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == ubuntu && ( ${VERSION_CODENAME:-} == noble || ${VERSION_CODENAME:-} == jammy ) ]] || die 'requires Ubuntu 22.04 or 24.04'
readonly ubuntu_codename=$VERSION_CODENAME

for command_name in apt-get awk curl dpkg-deb dpkg-query flock grep install readelf realpath sha256sum unzip; do
  command -v "$command_name" >/dev/null || die "missing prerequisite: $command_name"
done

prefix=$(realpath -m -- "$1")
case "$prefix" in
  /|/etc|/home|/opt|/root|/tmp|/usr|/var) die 'refusing a broad toolchain directory' ;;
esac
if [[ -e $prefix ]]; then
  [[ -d $prefix && $(stat -c '%u:%g:%a' "$prefix") == 0:0:755 ]] || die 'toolchain directory must be root-owned, mode 0755'
else
  install -d -m 0755 -- "$prefix"
fi
[[ ! -L $prefix/.toolchain.lock ]] || die 'toolchain lock must not be a symlink'
exec 9>"$prefix/.toolchain.lock"
flock -x 9

scratch=$(mktemp -d /tmp/xenoid-toolchain.XXXXXXXX)
sdk_stage=
cleanup() {
  rm -rf -- "$scratch"
  if [[ -n $sdk_stage ]]; then rm -rf -- "$sdk_stage"; fi
}
trap cleanup EXIT

java_root=/usr/lib/jvm/java-17-openjdk-arm64
[[ -x $java_root/bin/java && -x $java_root/bin/javac && -x $java_root/bin/keytool && -x $java_root/bin/jarsigner ]] || die 'install openjdk-17-jdk-headless'
"$java_root/bin/javac" -version 2>&1 | grep -E '^javac 17(\.|$)' >/dev/null || die 'Java 17 validation failed'

sdk_root=${XENOID_ANDROID_SDK_ROOT:-$prefix/android-sdk}
reuse_rust=${XENOID_REUSE_RUSTUP:-0}
if [[ $reuse_rust == 1 ]]; then
  cargo_root=${CARGO_HOME:-$HOME/.cargo}
  rustup_root=${RUSTUP_HOME:-$HOME/.rustup}
else
  cargo_root=$prefix/cargo
  rustup_root=$prefix/rustup
fi
for absolute_path in "$sdk_root" "$cargo_root" "$rustup_root"; do
  [[ $absolute_path =~ ^/[a-zA-Z0-9._/-]+$ && $absolute_path != / ]] || die 'tool roots must be absolute paths without shell metacharacters'
done

export JAVA_HOME=$java_root ANDROID_HOME=$sdk_root ANDROID_SDK_ROOT=$sdk_root
export CARGO_HOME=$cargo_root RUSTUP_HOME=$rustup_root
export PATH="$JAVA_HOME/bin:$CARGO_HOME/bin:$PATH"

fetch_verified() {
  local url=$1 expected=$2 destination=$3 actual
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 --connect-timeout 15 --max-time 600 --output "$destination" "$url"
  actual=$(sha256sum -- "$destination" | awk '{print $1}')
  [[ $actual == "$expected" ]] || die "SHA-256 mismatch for ${url##*/}"
}

is_amd64_elf() {
  [[ -f $1 ]] && LC_ALL=C readelf -h "$1" 2>/dev/null | grep -F 'Advanced Micro Devices X86-64' >/dev/null
}

download_amd64_zlib() {
  # Keep this metadata and package separate from the system's native APT sources.
  local apt_dir=$scratch/apt-amd64
  local keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg
  local -a apt_options
  local -a archives
  local extracted_library
  [[ -r $keyring ]] || die 'missing the Ubuntu archive signing keyring'
  chmod 0755 "$scratch"
  install -d -m 0755 "$apt_dir/lists/partial" "$apt_dir/cache/archives/partial" "$apt_dir/download"
  printf 'deb [arch=amd64 signed-by=%s] https://archive.ubuntu.com/ubuntu/ %s main\ndeb [arch=amd64 signed-by=%s] https://archive.ubuntu.com/ubuntu/ %s-updates main\ndeb [arch=amd64 signed-by=%s] https://security.ubuntu.com/ubuntu/ %s-security main\n' \
    "$keyring" "$ubuntu_codename" "$keyring" "$ubuntu_codename" "$keyring" "$ubuntu_codename" >"$apt_dir/sources.list"
  apt_options=(
    -o APT::Architecture=amd64
    -o APT::Architectures::=amd64
    -o "Dir::Etc::sourcelist=$apt_dir/sources.list"
    -o Dir::Etc::sourceparts=-
    -o "Dir::State::lists=$apt_dir/lists"
    -o "Dir::Cache=$apt_dir/cache"
    -o APT::Get::List-Cleanup=0
    -o APT::Update::Error-Mode=any
    -o Acquire::Languages=none
    -o Acquire::Retries=3
  )
  apt-get "${apt_options[@]}" update
  (cd "$apt_dir/download" && apt-get "${apt_options[@]}" download zlib1g:amd64)
  archives=("$apt_dir"/download/zlib1g_*_amd64.deb)
  [[ ${#archives[@]} -eq 1 && -f ${archives[0]} ]] || die 'could not fetch Ubuntu amd64 zlib1g'
  dpkg-deb -x "${archives[0]}" "$apt_dir/extracted"
  for extracted_library in "$apt_dir"/extracted/{lib,usr/lib}/x86_64-linux-gnu/libz.so.1; do
    if is_amd64_elf "$extracted_library"; then
      install -m 0644 -- "$extracted_library" "$amd64_library_dir/libz.so.1"
      return
    fi
  done
  die 'Ubuntu amd64 zlib1g did not contain the expected library'
}

ensure_amd64_compatibility() {
  local package library
  local handler=/proc/sys/fs/binfmt_misc/qemu-x86_64
  local cross_ld=$amd64_library_dir/ld-linux-x86-64.so.2
  local -a missing_packages=()
  # Android's pinned Linux SDK/NDK executables use a glibc x86-64 loader.
  for package in qemu-user-static libc6-amd64-cross libgcc-s1-amd64-cross libstdc++6-amd64-cross; do
    if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
      missing_packages+=("$package")
    fi
  done
  if [[ ${#missing_packages[@]} -ne 0 ]]; then
    note 'Installing Ubuntu x86-64 userspace compatibility for the Android SDK tools'
    apt-get -o APT::Update::Error-Mode=any update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing_packages[@]}"
  fi
  if [[ ! -r $handler ]] || ! grep -qx enabled "$handler"; then
    systemctl restart systemd-binfmt.service || die 'could not start the system binfmt registration service'
  fi
  [[ -r $handler ]] && grep -qx enabled "$handler" || die 'qemu-x86_64 binfmt handler is unavailable'
  is_amd64_elf "$cross_ld" || die 'Ubuntu amd64 cross loader is unavailable'

  if [[ ! -d /lib64 ]]; then install -d -m 0755 /lib64; fi
  if [[ ! -e /lib64/ld-linux-x86-64.so.2 && ! -L /lib64/ld-linux-x86-64.so.2 ]]; then
    ln -s -- "$cross_ld" /lib64/ld-linux-x86-64.so.2
  fi
  is_amd64_elf /lib64/ld-linux-x86-64.so.2 || die 'existing amd64 loader path is incompatible'
  if [[ ! -e /lib/x86_64-linux-gnu && ! -L /lib/x86_64-linux-gnu ]]; then
    ln -s -- "$amd64_library_dir" /lib/x86_64-linux-gnu
  fi
  [[ -d /lib/x86_64-linux-gnu ]] || die 'amd64 library directory is invalid'
  if ! is_amd64_elf /lib/x86_64-linux-gnu/libz.so.1; then
    [[ ! -e /lib/x86_64-linux-gnu/libz.so.1 && ! -L /lib/x86_64-linux-gnu/libz.so.1 ]] || die 'existing amd64 zlib path is incompatible'
    if ! is_amd64_elf "$amd64_library_dir/libz.so.1"; then
      [[ ! -e $amd64_library_dir/libz.so.1 && ! -L $amd64_library_dir/libz.so.1 ]] || die 'existing cross zlib path is incompatible'
      download_amd64_zlib
    fi
  fi
  for library in libc.so.6 libgcc_s.so.1 libstdc++.so.6 libz.so.1; do
    if [[ ! -e /lib/x86_64-linux-gnu/$library && ! -L /lib/x86_64-linux-gnu/$library ]]; then
      ln -s -- "$amd64_library_dir/$library" "/lib/x86_64-linux-gnu/$library"
    fi
    is_amd64_elf "/lib/x86_64-linux-gnu/$library" || die "expected amd64 library is incompatible: $library"
  done
}

sdk_ready() {
  local build_dir=$sdk_root/build-tools/$sdk_build_tools
  local ndk_dir=$sdk_root/ndk/$sdk_ndk/toolchains/llvm/prebuilt/linux-x86_64/bin
  [[ -f $sdk_root/platforms/$sdk_platform/android.jar && -x $build_dir/aapt2 && \
    -x $build_dir/apksigner && -x $build_dir/d8 && -x $build_dir/zipalign && \
    -x $ndk_dir/aarch64-linux-android33-clang && -x $ndk_dir/llvm-strip && \
    -x $sdk_root/cmake/$sdk_cmake/bin/cmake && -x $sdk_root/cmake/$sdk_cmake/bin/ninja ]] && \
    grep -Eq '^Pkg\.Revision[[:space:]]*=[[:space:]]*27\.2\.12479018[[:space:]]*$' "$sdk_root/ndk/$sdk_ndk/source.properties"
}

accept_android_terms() {
  local response
  [[ ${XENOID_ACCEPT_ANDROID_SDK_LICENSES:-0} != 1 ]] || return 0
  if { true </dev/tty; } 2>/dev/null; then
    printf '%s\n' 'Android SDK terms: https://developer.android.com/studio#terms-and-conditions' >&2
    read -r -p 'Do you accept the Android SDK licenses? [y/N] ' response </dev/tty || response=
    [[ $response == [yY] || $response == [yY][eE][sS] ]] && return 0
  fi
  die 'Android SDK licenses must be accepted. Re-run the installer with --accept-android-licenses.'
}

install_android_sdk() {
  local manager=$sdk_root/cmdline-tools/latest/bin/sdkmanager
  if ! sdk_ready; then
    [[ -z ${XENOID_ANDROID_SDK_ROOT:-} ]] || die 'existing Android SDK lacks the pinned required tools'
    accept_android_terms
    if [[ ! -x $manager ]]; then
      note 'Downloading Android SDK Command-line Tools from Google'
      fetch_verified "https://dl.google.com/android/repository/$sdk_cli_archive" "$sdk_cli_sha" "$scratch/$sdk_cli_archive"
      install -d -m 0755 -- "$sdk_root/cmdline-tools"
      sdk_stage=$(mktemp -d "$sdk_root/cmdline-tools/.stage.XXXXXXXX")
      unzip -q "$scratch/$sdk_cli_archive" -d "$sdk_stage"
      [[ -x $sdk_stage/cmdline-tools/bin/sdkmanager ]] || die 'invalid Android SDK Command-line Tools archive'
      [[ ! -e $sdk_root/cmdline-tools/latest && ! -L $sdk_root/cmdline-tools/latest ]] || die 'existing cmdline-tools/latest is incomplete'
      mv -T -- "$sdk_stage/cmdline-tools" "$sdk_root/cmdline-tools/latest"
    fi
    note 'Installing pinned Android SDK, NDK, and CMake packages'
    (
      set +o pipefail
      yes | "$manager" --sdk_root="$sdk_root" --install \
        "platforms;$sdk_platform" "build-tools;$sdk_build_tools" "ndk;$sdk_ndk" "cmake;$sdk_cmake"
    ) || die 'sdkmanager could not install the required Android packages'
  fi
  sdk_ready || die 'Android SDK installation is incomplete'
  "$sdk_root/build-tools/$sdk_build_tools/aapt2" version >/dev/null || die 'aapt2 failed to run through x86-64 compatibility'
  "$sdk_root/build-tools/$sdk_build_tools/apksigner" version >/dev/null || die 'apksigner failed to run'
  "$sdk_root/ndk/$sdk_ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android33-clang" --version >/dev/null || die 'Android NDK clang failed to run through x86-64 compatibility'
  "$sdk_root/cmake/$sdk_cmake/bin/cmake" --version >/dev/null || die 'Android SDK CMake failed to run'
  "$sdk_root/cmake/$sdk_cmake/bin/ninja" --version >/dev/null || die 'Android SDK Ninja failed to run'
}

rust_ready() {
  [[ -x $rustup_bin ]] && "$rustup_bin" run stable rustc --version >/dev/null 2>&1 && \
    "$rustup_bin" target list --toolchain stable --installed 2>/dev/null | grep -qx aarch64-linux-android
}

cargo_ndk_ready() {
  [[ -x $cargo_root/bin/cargo-ndk ]] && \
    "$rustup_bin" run stable cargo ndk --version 2>/dev/null | grep -Fx "cargo-ndk $cargo_ndk_release" >/dev/null
}

install_rust() {
  if [[ $reuse_rust == 1 ]]; then
    rustup_bin=$(command -v rustup || true)
    rust_ready && cargo_ndk_ready || die 'existing Rust needs stable, aarch64-linux-android, and cargo-ndk 4.1.2'
    return
  fi
  rustup_bin=$cargo_root/bin/rustup
  if [[ ! -x $rustup_bin ]]; then
    note 'Downloading Rustup from the Rust Project'
    fetch_verified "$rustup_archive_url" "$rustup_archive_sha" "$scratch/rustup-init"
    chmod 0755 "$scratch/rustup-init"
    RUSTUP_INIT_SKIP_PATH_CHECK=yes "$scratch/rustup-init" -y --no-modify-path --default-toolchain none
  fi
  if ! rust_ready; then
    "$rustup_bin" toolchain install stable --profile minimal --no-self-update --target aarch64-linux-android
  fi
  if ! cargo_ndk_ready; then
    "$rustup_bin" run stable cargo install --locked --version "$cargo_ndk_release" --root "$cargo_root" cargo-ndk
  fi
  rust_ready && cargo_ndk_ready || die 'Rust toolchain verification failed'
}

ensure_amd64_compatibility
install_android_sdk
install_rust

# Write only fixed assignments and prepend each path once on repeated sourcing.
install -d -o root -g root -m 0700 "$prefix/tmp"
env_tmp=$(mktemp "$prefix/.env.sh.XXXXXXXX")
{
  printf "export JAVA_HOME='%s'\nexport ANDROID_HOME='%s'\nexport ANDROID_SDK_ROOT='%s'\n" "$java_root" "$sdk_root" "$sdk_root"
  printf "export CARGO_HOME='%s'\nexport RUSTUP_HOME='%s'\n" "$cargo_root" "$rustup_root"
  # Xenoid's host-path redactor needs a prefix distinct from Android's /data/local/tmp.
  printf "export TEMP='%s'\n" "$prefix/tmp"
  printf '%s\n' 'if [ -n "${TMPDIR:-}" ] && [ "$(realpath -m -- "$TMPDIR")" = /tmp ]; then unset TMPDIR; fi'
  printf '%s\n' 'for xenoid_path in "$ANDROID_HOME/cmdline-tools/latest/bin" "$CARGO_HOME/bin" "$JAVA_HOME/bin"; do' \
    '  case ":$PATH:" in' '    *":$xenoid_path:"*) ;;' '    *) PATH="$xenoid_path${PATH:+:$PATH}" ;;' '  esac' 'done' \
    'unset xenoid_path' 'export PATH'
} >"$env_tmp"
chmod 0644 "$env_tmp"
mv -fT -- "$env_tmp" "$prefix/env.sh"
note 'Android and Rust toolchains verified'
