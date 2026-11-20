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
    def regenerate(self, timeout=0, operation_key=None):
        """Regenerate/reload through official Xenoid, preserving the container.

        Zero waits without a deadline. Retain an explicit operation_key when
        retrying an uncertain request; a new call otherwise gets a fresh key.
        Display tuning remains a separate script-controlled step.
        """
        key = uuid.uuid4().hex if operation_key is None else operation_key
        return request("regenerate", [timeout, key])

    def set_frame_rate(self, fps, timeout=None):
        """Set integral display FPS; None preserves it, 0 restores native modes.

        A changed rate restarts Android graphics/apps. Call before app_open().
        Repeating an already active rate does not restart Android. The optional
        timeout overrides command_timeout for setup only; 0 means unlimited.
        """
        if timeout is not None:
            return request("set-frame-rate", [fps, timeout])
        return request("set-frame-rate", [fps])

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

    def inspect_window(self, package=None, priority=None):
        """Read focus/UI; optional -20..19 niceness applies only to this inspection."""
        if package is not None and not isinstance(package, str):
            raise ValueError("Supply an Android package name")
        if priority is not None:
            return request("inspect-window", [package, priority])
        return request("inspect-window", [] if package is None else [package])

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
    if action == "regenerate" and len(args) <= 2:
        return Device().regenerate(int(args[0]) if args else 0, args[1] if len(args) == 2 else None)
    if action == "page-json":
        return json.dumps(Device().page_json(int(args[0]) if args else 35), ensure_ascii=False)
    if action == "tap" and len(args) == 2:
        return Device().tap(*args)
    if action == "set-frame-rate" and 1 <= len(args) <= 2:
        return Device().set_frame_rate(None if args[0] == "none" else int(args[0]),
                                       timeout=int(args[1]) if len(args) == 2 else None)
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
