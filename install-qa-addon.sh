#!/usr/bin/env bash
# Install the verified source-only QA addon on an existing Fleet host.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }
qa_source=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
(
    cd "$qa_source"
    sha256sum --check --status qa/manifest.sha256
)
qa_target=/opt/xenoid-fleet/panel-scripts
install -d -o root -g root -m 0755 "$qa_target"
exec 8>"$qa_target/.installer-lock"
flock 8
# Publish host dependencies before the entrypoint that imports them.
for qa_file in qa_vpn_probe.py qa_lifecycle.py qa_input_ready.py qa_host_memory.py qa_control_routes.py qa_ui_reader.py qa_google_reset.py qa_fresh6a.py qa_device_profile.py qa_profile_runtime.py qa_resource_runtime.py qa_memory_persistence.py qa_resources.py script_runtime.py shared_policy.py script_host.py shared-app-script.py vpn-cycle.py qa_proxy_routing.py restore-display.py restore-default-display.py restore-host-memory.py; do
    qa_temp=$(mktemp "$qa_target/.install.XXXXXXXX")
    install -o root -g root -m 0755 "$qa_source/qa/$qa_file" "$qa_temp"
    mv -f "$qa_temp" "$qa_target/$qa_file"
done
qa_temp=$(mktemp "$qa_target/.install.XXXXXXXX")
install -o root -g root -m 0644 "$qa_source/qa/default-bluejay-policy.json" "$qa_temp"
mv -f "$qa_temp" "$qa_target/default-bluejay-policy.json"
install -d -o root -g root -m 0700 /etc/xenoid-fleet /var/lib/xenoid-fleet/qa-resources /var/lib/xenoid-fleet/device-profiles
if [[ ! -e /etc/xenoid-fleet/qa-resources.json ]]; then
    install -o root -g root -m 0600 "$qa_source/qa/qa-resources.json" /etc/xenoid-fleet/qa-resources.json
fi
if [[ ! -e /etc/xenoid-fleet/device-profile-policy.json && ! -L /etc/xenoid-fleet/device-profile-policy.json ]]; then
    qa_temp=$(mktemp /etc/xenoid-fleet/.device-profile-policy.XXXXXXXX)
    if python3 "$qa_target/qa_fresh6a.py" emit-policy \
        --template /opt/xenoid-fleet/upstream/examples/fingerprints/pixel-bluejay-android13.json > "$qa_temp"; then
        chmod 0600 "$qa_temp"
        mv -f "$qa_temp" /etc/xenoid-fleet/device-profile-policy.json
    else
        rm -f "$qa_temp"
        exit 1
    fi
fi
if [[ ! -e /etc/default/xenoid-qa-zram ]]; then
    install -o root -g root -m 0644 "$qa_source/deploy/xenoid-qa-zram.default" /etc/default/xenoid-qa-zram
fi
install -o root -g root -m 0755 "$qa_source/deploy/xenoid-qa-zram" /usr/local/sbin/xenoid-qa-zram
for qa_unit in xenoid-qa-zram.service xenoid-qa-resources.service xenoid-qa-resources.timer; do
    install -o root -g root -m 0644 "$qa_source/deploy/$qa_unit" "/etc/systemd/system/$qa_unit"
done
python3 "$qa_target/qa_proxy_routing.py" ensure-default
python3 "$qa_target/qa_memory_persistence.py" --apply
systemctl daemon-reload
python3 "$qa_target/qa_memory_persistence.py" --verify
systemctl enable --now xenoid-qa-zram.service xenoid-qa-resources.timer
sha256sum "$qa_source/qa/manifest.sha256" | cut -d' ' -f1 >/opt/xenoid-fleet/.qa-addon-installed
chmod 0644 /opt/xenoid-fleet/.qa-addon-installed
printf 'Installed configurable QA defaults; existing settings were retained.\n'
