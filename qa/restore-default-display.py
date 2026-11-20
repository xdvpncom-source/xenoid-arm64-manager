"""Restore the original fleet display from the panel; this is a single run.

Before running this file, switch off automatic resource defaults in the panel
(Apps → device resources), save and apply that setting to all relevant hosts.
STOP or replace the low-display shared script as well. Starting that workflow
again reapplies its minimum resolution/DPI and 20 FPS.

FPS 0 restores the native 60/120 Hz modes. The compatible HWC library bind stays
installed; the separate host rollback removes that bind/library physically.
This file changes only display settings: it does not launch or clear an app.
"""

import re

from xenoid_device import Device


RUN_SETTINGS = {"repeat_count": 1, "timeout": 0, "command_timeout": 60}
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 3120
DEFAULT_DPI = 560
DISPLAY_SETUP_TIMEOUT = 180


def effective_display(device):
    size_output = device.adb(["shell", "wm", "size"])
    density_output = device.adb(["shell", "wm", "density"])
    sizes = re.findall(r"(?m)^(Physical|Override) size:\s*(\d+)x(\d+)\s*$", size_output)
    densities = re.findall(r"(?m)^(Physical|Override) density:\s*(\d+)\s*$", density_output)
    physical_size = [tuple(map(int, row[1:])) for row in sizes if row[0] == "Physical"]
    override_size = [tuple(map(int, row[1:])) for row in sizes if row[0] == "Override"]
    physical_dpi = [int(row[1]) for row in densities if row[0] == "Physical"]
    override_dpi = [int(row[1]) for row in densities if row[0] == "Override"]
    if (len(physical_size) != 1 or len(physical_dpi) != 1
            or len(override_size) > 1 or len(override_dpi) > 1):
        raise RuntimeError("Android did not report an unambiguous display size and density")
    return (override_size or physical_size)[0], (override_dpi or physical_dpi)[0]


def main():
    if any(type(value) is not int or value <= 0
           for value in (DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_DPI)):
        raise ValueError("DEFAULT_WIDTH, DEFAULT_HEIGHT and DEFAULT_DPI must be positive integers")
    if type(DISPLAY_SETUP_TIMEOUT) is not int or DISPLAY_SETUP_TIMEOUT < 0:
        raise ValueError("DISPLAY_SETUP_TIMEOUT must be a nonnegative integer; 0 means unlimited")
    device = Device()
    device.set_frame_rate(0, timeout=DISPLAY_SETUP_TIMEOUT)
    device.log("Native display refresh modes restored (60/120 Hz)")
    device.adb(["shell", "wm", "size", f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}"])
    device.adb(["shell", "wm", "density", str(DEFAULT_DPI)])
    size, dpi = effective_display(device)
    if size != (DEFAULT_WIDTH, DEFAULT_HEIGHT) or dpi != DEFAULT_DPI:
        raise RuntimeError(f"Display restore was not confirmed: effective {size[0]}x{size[1]}@{dpi}")
    device.log(f"Default display restored and verified: {size[0]}x{size[1]}@{dpi}; native refresh modes")


if __name__ == "__main__":
    main()
