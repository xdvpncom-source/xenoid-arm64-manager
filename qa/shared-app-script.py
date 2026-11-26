"""Panel-controlled device automation. Existing scripts may keep their own Device client."""

from xenoid_device import Device as RuntimeDevice


# Optional script overrides. Omit this dictionary to use the panel form.
# RUN_SETTINGS = {
#     "repeat_count": 0,       # 0 = repeat until stopped
#     "timeout": 0,            # seconds per round; 0 = unlimited
#     "initial_delay": 0,
#     "interval": 5,
#     "command_timeout": 0,    # 0 = unlimited ADB command time
#     "verify_apk": False,
#     "android_user": None,    # None = active Android user
#     "package": None,         # explicit default package, or autodetect
# }


class Device(RuntimeDevice):
    """Full SDK supplied by the host; methods also work in previously saved scripts.

    Any window: tap/click(x, y), find(...).click(), app_tap(resource_id),
    inspect_window(), ui_dump(), back(), keyevent(), swipe(), text().
    Controls: clickable_controls(window) lists every observed clickable entry;
    click_control(entry, window=window) rechecks it before official native input.
    Dialogs: app_confirm(), confirm_vpn(), close_popup(), detect_window(),
    click_window_action(window, action).
    Apps/ADB: app_package(), app_open(), app_close(), app_reset(),
    clear_data(package), app_grant_permission(name), adb(args, timeout=0),
    shell(command, timeout=0).
    Display: set_frame_rate(fps) before app_open(); None preserves the current
    rate, 0 restores native display modes. Changing FPS restarts Android apps.
    Scheduling: settings(), configure_run(repeat_count=0, timeout=0, ...),
    sleep(seconds). Legacy V2 helpers remain available.
    Official identity reload: regenerate(timeout=0, operation_key=key). Keep the
    same key for retries; reapply your display settings after it completes.
    """


def run(device):
    # This starter only logs settings. Add your own steps here.
    device.log(f"Run settings: {device.settings()}")

    # Examples (uncomment and adapt):
    # device.set_frame_rate(20, timeout=180)  # Setup budget only; 0 = unlimited.
    # device.regenerate(timeout=0, operation_key="my-retained-operation-key")
    # device.app_open()
    # device.tap(540, 960)
    # device.find(resource_id="com.android.vpndialogs:id/always").click()
    # device.confirm_vpn()
    # device.close_popup()
    # device.back()
    # device.app_grant_permission("android.permission.CAMERA")
    # device.clear_data("com.example.app")
    # device.adb(["shell", "pm", "list", "packages"])
    # device.shell("input keyevent KEYCODE_HOME", timeout=0)
    # device.configure_run(repeat_count=10, interval=3, timeout=0)
    # device.sleep(2)


if __name__ == "__main__":
    run(Device())
