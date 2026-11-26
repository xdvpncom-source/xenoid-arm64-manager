"""Restore this device's original UI preferences without regenerating identity.

First disable UI_PREFERENCES in the shared scenario if future GENs should keep
the restored settings. Run this file once through the panel on desired devices.
"""
from xenoid_device import Device

RUN_SETTINGS = {"repeat_count": 1, "timeout": 0, "verify_apk": False}

if __name__ == "__main__":
    device = Device()
    result = device.restore_preferences()
    if not result.get("ok") or not result.get("restored") or not result.get("verified"):
        raise RuntimeError("Original Android user preferences were not verified")
    device.log("Original Android locale, timezone, date/time format, font and theme restored")
