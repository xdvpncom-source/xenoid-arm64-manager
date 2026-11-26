#!/usr/bin/python3
"""Device-scoped SDK for managed Xenoid Python and Bash scripts."""

import json
import os
import re
import socket
import shutil
import shlex
import time
import sys
import uuid
from xml.etree import ElementTree


def request(action, args):
    with socket.socket(socket.AF_UNIX) as client:
        seconds = os.environ.get("XENOID_REQUEST_TIMEOUT", "0")
        client.settimeout(float(seconds) or None)
        client.connect("/work/device.sock")
        client.sendall(json.dumps({"action": action, "args": args}).encode() + b"\n")
        with client.makefile("rb") as stream:
            line = stream.readline()
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Device request failed"))
    return response["value"]


class Element:
    def __init__(self, device, node):
        self.device = device
        self.attributes = dict(node.attrib)

    def click(self):
        match = re.fullmatch(r"\[(-?[0-9]+),(-?[0-9]+)\]\[(-?[0-9]+),(-?[0-9]+)\]", self.attributes.get("bounds", ""))
        if not match:
            raise ValueError("The selected element has no screen bounds")
        left, top, right, bottom = map(int, match.groups())
        if right <= left or bottom <= top:
            raise ValueError("The selected element is outside the visible screen")
        return self.device.tap((left + right) // 2, (top + bottom) // 2)


class Device:
    def input_status(self, timeout=None):
        """Read official input readiness and foreground without touching/repairing.

        The script decides which package/window to act on. Zero timeout waits
        without a deadline; None uses the panel's command_timeout setting.
        """
        return request("input-status", [] if timeout is None else [timeout])

    def clear_cache(self, package, *, user="current", timeout=None):
        """Clear only this package's cache through Android PackageManager.

        Application data, preferences and identity stores are preserved. This
        is script-controlled and is never an implicit regeneration hook.
        """
        return request("clear-cache", [package, user, timeout])

    def regenerate(self, timeout=0, operation_key=None, *, post_cache=None, post_google=None,
                   post_preferences=None):
        """Regenerate/reload through official Xenoid, preserving the container.

        Zero waits without a deadline. Retain an explicit operation_key when
        retrying an uncertain request; a new call otherwise gets a fresh key.
        Display tuning remains a separate script-controlled step.
        Optional post_cache={packages:[...],user:'current',timeout:0} is saved
        before native work and completed durably before this operation returns.
        Optional post_google is a separate, explicit data-reset/registration plan;
        it never changes clear_cache into a full-data operation.
        Optional post_preferences selects and verifies real Android regional,
        font and theme settings once per GEN, with a saved rollback baseline.
        """
        key = uuid.uuid4().hex if operation_key is None else operation_key
        if post_preferences is not None:
            return request("regenerate", [timeout, key, post_cache, post_google, post_preferences])
        if post_google is not None:
            return request("regenerate", [timeout, key, post_cache, post_google])
        return request("regenerate", [timeout, key] if post_cache is None else [timeout, key, post_cache])

    def preferences_snapshot(self):
        """Read the actual Android user preferences without changing them."""
        return request("ui-preferences-read", [])

    def restore_preferences(self):
        """Restore this device's first captured preference baseline; no GEN."""
        return request("ui-preferences-restore", [])

    def preferences_baseline(self):
        """Read the original UI settings and native country saved for rollback."""
        return request("ui-preferences-baseline", [])

    def set_frame_rate(self, fps, timeout=None):
        """Set integral display FPS; None preserves it, 0 restores native modes.

        A changed rate restarts Android graphics/apps. Call before app_open().
        Repeating an already active rate does not restart Android. The optional
        timeout overrides command_timeout for setup only; 0 means unlimited.
        """
        if timeout is not None:
            return request("set-frame-rate", [fps, timeout])
        return request("set-frame-rate", [fps])

    def apply_resource_defaults(self, timeout=None):
        """Apply the panel's current resource policy and generated profile size.

        With no explicit timeout the panel's setup budget is used. Zero waits
        without a deadline. This does not inject touch input or regenerate IDs.
        """
        return request("apply-resource-defaults", [] if timeout is None else [timeout])

    def app_package(self):
        """Return the configured/assigned package, or the current foreground package."""
        return request("app-package", [])

    def app_open(self):
        return request("app-open", [])

    def app_close(self):
        return request("app-close", [])

    def app_tap(self, resource_id):
        """Click a unique resource ID in the current hierarchy, including other packages."""
        return request("app-tap", [resource_id])

    def app_confirm(self):
        """Click the system/native positive dialog button in any package."""
        return request("app-confirm", [])

    def app_grant_permission(self, permission):
        """Grant the requested permission to the configured app."""
        return request("app-grant-permission", [permission])

    def app_reset(self):
        """Clear the configured app data."""
        return request("app-reset", [])

    def inspect_window(self, package=None, priority=None, windows=False, reader=None):
        """Read UI; reader='legacy' explicitly uses the per-call stock launcher."""
        if package is not None and not isinstance(package, str):
            raise ValueError("Supply an Android package name")
        if type(windows) is not bool:
            raise ValueError("windows must be Boolean")
        if reader not in (None, "persistent", "legacy"):
            raise ValueError("reader must be persistent or legacy")
        if reader is not None:
            return request("inspect-window", [package, priority, windows, reader])
        if windows:
            return request("inspect-window", [package, priority, True])
        if priority is not None:
            return request("inspect-window", [package, priority])
        return request("inspect-window", [] if package is None else [package])

    def clickable_controls(self, window=None, *, priority=None):
        """List observed clickable controls, including disabled entries.

        Indices describe this snapshot only. Disabled/background controls are
        never click targets; no input is sent by enumeration. All original
        window metadata is retained alongside readable labels and bounds.
        """
        if window is None:
            window = self.inspect_window(priority=priority, windows=True)
        if not isinstance(window, dict):
            raise ValueError("Supply an inspected window")
        result, seen = [], set()
        for source in ("controls", "navigation_controls"):
            for item in window.get(source, ()):
                if (not isinstance(item, dict) or item.get("clickable") is not True
                        or source == "controls" and item.get("window_active") is False
                        and item.get("window_focused") is False):
                    continue
                row = dict(item)
                for key in ("text", "content_desc", "resource_id", "class_name"):
                    row.setdefault(key, "")
                row.setdefault("package", window.get("package", ""))
                row.setdefault("display_id", window.get("display_id"))
                for key in ("window_id", "window_layer", "window_type", "window_title", "window_bounds",
                            "window_active", "window_focused"):
                    row.setdefault(key, None)
                row.setdefault("bounds", [])
                row.setdefault("enabled", False)
                row["bounds"] = list(row["bounds"])
                identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
                if identity in seen:
                    continue
                seen.add(identity)
                row["index"] = len(result) + 1
                result.append(row)
        return result

    def click_control(self, control, *, window, priority=None):
        """Re-read an enumerated control before one official native tap.

        The old index alone never identifies a new target. A changed foreground,
        moved/disabled/missing or ambiguous control is rejected without input.
        """
        if (not isinstance(window, dict) or window.get("stable") is not True
                or not isinstance(control, dict) or control.get("enabled") is not True
                or control.get("clickable") is not True):
            raise ValueError("Select an enabled clickable control from a stable window")
        expected = {key: value for key, value in control.items() if key != "index"}
        original = [{key: value for key, value in item.items() if key != "index"}
                    for item in self.clickable_controls(window)]
        if expected not in original:
            raise ValueError("The control does not belong to this window snapshot")
        fresh = self.inspect_window(priority=priority, windows=True)
        if (fresh.get("stable") is not True or any(fresh.get(key) != window.get(key)
                for key in ("package", "component", "display_id"))):
            raise RuntimeError("The foreground window changed before the control could be clicked")
        matches = [item for item in self.clickable_controls(fresh)
                   if {key: value for key, value in item.items() if key != "index"} == expected]
        if len(matches) != 1:
            raise RuntimeError("The selected control changed or is ambiguous")
        box = matches[0]["bounds"]
        if (len(box) != 4 or any(type(value) is not int for value in box)
                or box[2] <= box[0] or box[3] <= box[1]):
            raise ValueError("The selected control has no visible bounds")
        return self.tap((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)

    def detect_window(self):
        """Read the current foreground and its available named actions."""
        return request("detect-window", [])

    def click_window_action(self, window, action):
        """Select a named action from the most recent observation on this device."""
        if not isinstance(window, dict) or not isinstance(action, str) or not action:
            raise ValueError("Supply a window returned by detect_window() and an action name")
        observation = window.get("observation")
        if not isinstance(observation, str) or not observation:
            raise ValueError("The window is missing its observation token")
        return request("handle-window", [observation, action])

    def run_v2_qa_cycle(self, **options):
        """Run the legacy workflow; timing and cleanup options belong to the script."""
        return request("qa-cycle-v4", [options] if options else [])

    def open_qa_app(self):
        return request("qa-open", [])

    def dismiss_qa_overlay(self):
        return request("qa-dismiss", [])

    def dismiss_v2_test_overlay(self):
        return request("qa-dismiss-v2", [])

    def tap_qa_center_control(self):
        return request("qa-center", [])

    def connect_v2_with_permission(self):
        return request("qa-center-v2", [])

    def check_v2_connection(self):
        return request("qa-connect-check-v3", [])

    def finish_v2_connection_test(self):
        return request("qa-finish-v3", [])

    def finish_qa_session(self):
        return request("qa-finish", [])

    def adb(self, args, timeout=None):
        """Run any ADB command on this script's device; 0 timeout means unlimited."""
        values = shlex.split(args) if isinstance(args, str) else list(args)
        if timeout is None:
            return request("adb", values)
        return request("adb-command", [values, timeout])

    def shell(self, command, timeout=None):
        values = [command] if isinstance(command, str) else list(command)
        return self.adb(["shell", *values], timeout=timeout)

    def keyevent(self, key):
        return self.adb(["shell", "input", "keyevent", str(key)])

    def back(self):
        return self.keyevent("KEYCODE_BACK")

    def swipe(self, x1, y1, x2, y2, duration=None):
        args = ["shell", "input", "swipe", *map(str, (x1, y1, x2, y2))]
        if duration is not None:
            args.append(str(duration))
        return self.adb(args)

    def text(self, value):
        return self.shell("input text " + shlex.quote(str(value)))

    def clear_data(self, package=None, user="current"):
        if package is None:
            return self.app_reset()
        return self.adb(["shell", "pm", "clear", "--user", str(user), package])

    def confirm_vpn(self):
        return self.app_confirm()

    def close_popup(self):
        window = self.detect_window()
        names = {item["name"] for item in window["actions"]}
        return self.click_window_action(window, "close" if "close" in names else "back")

    def settings(self):
        return request("settings", [])

    def configure_run(self, **settings):
        """Update this run's scheduling/options. Counts/timeouts of 0 are unlimited."""
        return request("configure-run", [settings])

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)


    def open_url(self, url):
        return request("open-url", [url])

    def page_json(self, timeout=35):
        return request("page-json", [timeout])

    def close_chrome(self):
        return request("close-chrome", [])

    def ui_dump(self):
        return request("ui-dump", [])

    def tap(self, x, y):
        return request("tap", [int(x), int(y)])

    click = tap

    def find(self, *, text=None, content_desc=None, resource_id=None, package=None):
        selectors = {key: value for key, value in (
            ("text", text), ("content-desc", content_desc), ("resource-id", resource_id), ("package", package)
        ) if value is not None}
        if not selectors:
            raise ValueError("Supply at least one UI selector")
        for node in ElementTree.fromstring(self.ui_dump()).iter("node"):
            if node.get("enabled") != "false" and all(node.get(key) == value for key, value in selectors.items()):
                return Element(self, node)
        return None

    @staticmethod
    def log(message):
        print(message, flush=True)


def cli(program, args):
    if program == "xenoid":
        if args[:2] == ["device", "regenerate"]:
            return cli("device", ["regenerate", *args[2:]])
        if args[:1] != ["adb"]:
            raise ValueError("Use xenoid adb or xenoid device regenerate for this device")
        program, args = "adb", args[1:]
    if program == "adb":
        if args[:1] == ["-s"]:
            if len(args) < 2 or args[1] != os.environ["ANDROID_SERIAL"]:
                raise ValueError("ADB target differs from this script's assigned device")
            args = args[2:]
        return Device().adb(args)
    if not args:
        raise ValueError("Usage: device open-url|page-json|close-chrome|ui-dump|tap|adb ...")
    action, *args = args
    if action == "regenerate" and len(args) <= 4:
        return Device().regenerate(int(args[0]) if args else 0, args[1] if len(args) >= 2 else None,
                                   post_cache=json.loads(args[2]) if len(args) >= 3 else None,
                                   post_google=json.loads(args[3]) if len(args) == 4 else None)
    if action == "input-status" and len(args) <= 1:
        return Device().input_status(int(args[0]) if args else None)
    if action == "page-json":
        return json.dumps(Device().page_json(int(args[0]) if args else 35), ensure_ascii=False)
    if action == "tap" and len(args) == 2:
        return Device().tap(*args)
    if action == "set-frame-rate" and 1 <= len(args) <= 2:
        return Device().set_frame_rate(None if args[0] == "none" else int(args[0]),
                                       timeout=int(args[1]) if len(args) == 2 else None)
    if action == "apply-resource-defaults" and len(args) <= 1:
        return Device().apply_resource_defaults(int(args[0]) if args else None)
    return request(action, args)


def launch(language, filename):
    # /run and /var may be noexec on the host. This tiny private tmpfs exists only in the sandbox.
    # Read the user source with the interpreter and keep executable SDK shims on their own mount.
    directory = "/work/bin"
    shim = directory + "/xenoid_device.py"
    shutil.copyfile("/work/xenoid_device.py", shim)
    os.chmod(shim, 0o500)
    for name in ("adb", "device", "xenoid"):
        os.symlink("xenoid_device.py", directory + "/" + name)
    os.environ["PATH"] = directory + ":/usr/bin:/bin"
    if language == "python":
        args = ["/usr/bin/python3", "-s", filename]
    else:
        args = ["/bin/bash", "--noprofile", "--norc", filename]
    os.execv(args[0], args)


if __name__ == "__main__":
    try:
        if os.path.basename(sys.argv[0]) == "xenoid_device.py" and len(sys.argv) == 4 and sys.argv[1] == "--launch":
            launch(sys.argv[2], sys.argv[3])
        output = cli(os.path.basename(sys.argv[0]), sys.argv[1:])
        if output is not None:
            print(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
