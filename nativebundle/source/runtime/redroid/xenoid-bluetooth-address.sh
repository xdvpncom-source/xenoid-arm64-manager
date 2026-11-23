#!/system/bin/sh
# Replay only an existing native-owned target. Never generate an identity here.
set -eu
setprop vendor.bt.xenoid_address_ready 0
profile=/data/local/tmp/xenoid-profile
path=$profile/bluetooth_address
address=
if test -e "$path" || test -L "$path"; then
    test -d "$profile" && test ! -L "$profile"
    case "$(stat -c '%u:%g:%a' "$profile")" in
        0:0:700|0:0:755) ;;
        *) echo 'xenoid-bluetooth-address:unsafe-profile-directory' >&2; exit 1 ;;
    esac
    test -f "$path" && test ! -L "$path"
    test "$(stat -c '%u:%g:%a:%h' "$path")" = 0:0:600:1
    test "$(stat -c '%s' "$path")" = 18
    address=$(cat "$path")
else
    # Recover a prior native setter's durable property without selecting a new
    # address. A fresh instance is seeded by the host before Android starts.
    address=$(getprop persist.vendor.bt.rootcanal_mac_address)
fi
case "$address" in
    [0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]) ;;
    *) echo 'xenoid-bluetooth-address:invalid-or-missing-target' >&2; exit 1 ;;
esac
# Persisted properties are loaded during post-fs-data. Do not race the loader;
# the ready-property action below replays the same sidecar after loading ends.
if test "$(getprop ro.persistent_properties.ready)" = true; then
    if test "$(getprop persist.vendor.bt.rootcanal_mac_address)" != "$address"; then
        setprop persist.vendor.bt.rootcanal_mac_address "$address"
    fi
    test "$(getprop persist.vendor.bt.rootcanal_mac_address)" = "$address"
fi
if test "$(getprop vendor.bt.rootcanal_mac_address)" != "$address"; then
    setprop vendor.bt.rootcanal_mac_address "$address"
fi
test "$(getprop vendor.bt.rootcanal_mac_address)" = "$address"
setprop vendor.bt.xenoid_address_ready 1
