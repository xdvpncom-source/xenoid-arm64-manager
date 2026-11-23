package dev.xenoid.daemon;

import java.util.Locale;

/** The native transaction owns the address; Rootcanal consumes its property. */
final class BluetoothIdentity {
    private BluetoothIdentity() {}

    static String normalize(String value) {
        String address = value == null ? "" : value.trim().toUpperCase(Locale.ROOT);
        if (!address.matches("[0-9A-F]{2}(:[0-9A-F]{2}){5}")) {
            throw new IllegalArgumentException("bluetooth_address_invalid");
        }
        return address;
    }

    static String publishCommand(String value) {
        String address = normalize(value);
        // All interpolated bytes are constrained to uppercase hex and colons.
        // The sidecar is published first: an interrupted setter must retain
        // this exact transaction target for reboot and idempotent replay.
        return "set -eu; setprop vendor.bt.xenoid_address_ready 0; /system/bin/xenoid-profile-helper set-bluetooth-address '" + address + "'; "
                + "setprop persist.vendor.bt.rootcanal_mac_address '" + address + "'; "
                + "setprop vendor.bt.rootcanal_mac_address '" + address + "'; "
                + "test \"$(getprop persist.vendor.bt.rootcanal_mac_address)\" = '" + address + "'; "
                + "test \"$(getprop vendor.bt.rootcanal_mac_address)\" = '" + address + "'; "
                + "settings --user 0 put secure bluetooth_address '" + address + "'; "
                + "settings --user 0 put secure bluetooth_addr_valid 1; "
                + "setprop vendor.bt.xenoid_address_ready 1";
    }
}
