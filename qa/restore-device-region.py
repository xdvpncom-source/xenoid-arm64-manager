"""Restore the original region through official GEN, then original UI settings.

Disable UI_PREFERENCES in the shared scenario first. Country restoration uses
fresh SIM/device identifiers; it never reinstates identifiers already retired
by the fleet's uniqueness ledger. The APK and container are retained.
"""
import time
import uuid

from xenoid_device import Device

RUN_SETTINGS = {"repeat_count": 1, "timeout": 0, "verify_apk": False}


def main():
    device = Device()
    baseline = device.preferences_baseline()
    snapshot = baseline["snapshot"]
    country = baseline.get("native_country")
    if country is not None:
        original = snapshot["values"]
        system = snapshot["settings"]["system"]
        plan = {"enabled": True, "regions": [{
            "locales": original["locales"], "timezone": original["timezone"],
            "time_12_24": system["time_12_24"] or "24",
            "date_format": system["date_format"] or "yyyy-MM-dd"}],
            "font_scales": [original["font_scale"]],
            "night_modes": [original["night_mode"] if original["night_mode"] in (1, 2) else 1],
            "native_location": {"country": country, "randomize_carrier": False}}
        key = "restore-region-" + uuid.uuid4().hex
        while True:
            try:
                result = device.regenerate(operation_key=key, post_preferences=plan,
                    post_cache={"packages": ["com.google.android.gms", "com.google.android.gsf"],
                                "user": "current", "timeout": 0},
                    post_google={"clear_packages": ["com.google.android.gms", "com.google.android.gsf"],
                                 "user": "current", "delay_seconds": 2, "timeout": 0})
                if not result.get("ok") or not result.get("ui_preferences", {}).get("verified"):
                    raise RuntimeError("Original region regeneration was not verified")
                break
            except (RuntimeError, OSError) as error:
                device.log("Waiting for the same region-restore operation: " + str(error))
                time.sleep(15)
    result = device.restore_preferences()
    if not result.get("ok") or not result.get("restored") or not result.get("verified"):
        raise RuntimeError("Original user settings were not restored")
    device.log("Original country and user settings restored with fresh unique identifiers"
               if country is not None else "Original user settings restored; native country unchanged")


if __name__ == "__main__":
    main()
