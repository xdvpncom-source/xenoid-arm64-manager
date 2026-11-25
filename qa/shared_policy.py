"""Input validation and UI helpers for administrator-controlled device scripts.

Operation selection, target packages and run limits belong to panel settings and
the script.  These helpers validate data; they do not impose an action policy.
"""

import re
from xml.etree import ElementTree


# Historical identifiers remain import-compatible; they are not deny lists.
RETIRED_APK_SHA256 = "c4eefd5af81351ca434984a46c3ad2eb49dd56ed9e5e5da1a74d86b58c781a50"
RETIRED_PACKAGE = "v2.vpn.fast"
# Zero means unlimited for repeat_count and timeout.
SHARED_TIMING = {"repeat_count": 0, "initial_delay": 0, "interval": 0, "timeout": 0}
SHA256 = re.compile(r"[0-9a-f]{64}")
INSTANCE_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
RESOURCE_TAG = re.compile(r"[0-9a-f]{12}")
ANDROID_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
APK_PATH = re.compile(r"package:(/[^\s\0]+\.apk)")
APK_LIST_ROW = re.compile(APK_PATH.pattern + r"=(" + ANDROID_PACKAGE.pattern + r")")
APP_ACTIONS = {
    "app-package", "app-open", "app-close", "app-tap", "app-confirm", "app-grant-permission", "app-reset",
}
ANDROID_BOUNDS = re.compile(r"\[(-?[0-9]+),(-?[0-9]+)\]\[(-?[0-9]+),(-?[0-9]+)\]")


def window_snapshot(hierarchy, display_id=0):
    """Decode the platform's --windows dump without confusing background roots.

    All windows remain available to the administrator's script. ``controls``
    describes the active/focused windows, while navigation is separate so a
    script can deliberately select an observed SystemUI control.
    """
    if type(display_id) is not int or display_id < 0:
        raise ValueError("Supply a nonnegative display ID")
    root = ElementTree.fromstring(hierarchy)
    if root.tag == "hierarchy":
        controls, truncated = shared_policy("visible_controls", hierarchy=hierarchy)
        return {"controls": controls, "navigation_controls": [], "windows": [],
                "display_id": display_id, "truncated": truncated}
    if root.tag != "displays":
        raise ValueError("Invalid Android window hierarchy")
    displays = [d for d in root if d.tag == "display" and d.get("id") == str(display_id)]
    if len(displays) != 1:
        raise ValueError("Expected one hierarchy for the selected Android display")
    windows, active, navigation = [], [], []
    for window in displays[0].iter("window"):
        box = ANDROID_BOUNDS.fullmatch(window.get("bounds", ""))
        # Stock Android --windows places its root under a direct hierarchy
        # wrapper. Older readers can put the node directly under this window.
        # Never borrow a descendant child-window root or resolve mixed roots.
        direct_roots = [child for child in window if child.tag == "node"]
        hierarchies = [child for child in window if child.tag == "hierarchy"]
        wrapped_roots = [child for hierarchy in hierarchies for child in hierarchy
                         if child.tag == "node"]
        roots = direct_roots + wrapped_roots
        unambiguous_root = ((len(direct_roots) == 1 and not hierarchies)
                            or (not direct_roots and len(hierarchies) == 1 and len(wrapped_roots) == 1))
        root_package = roots[0].get("package", "") if unambiguous_root else ""
        root_package = root_package if ANDROID_PACKAGE.fullmatch(root_package) else ""
        metadata = {"display_id": display_id,
                    "window_id": int(window.get("id")) if window.get("id") is not None else None,
                    "window_layer": int(window.get("layer", "0")),
                    "window_type": window.get("type", ""),
                    "window_title": window.get("title", ""),
                    "window_bounds": list(map(int, box.groups())) if box else None,
                    "window_active": window.get("active") == "true",
                    "window_focused": window.get("focused") == "true"}
        controls = []
        raw_node_count = 0
        stack = list(reversed(list(window)))
        while stack:
            node = stack.pop()
            if node.tag == "window":
                continue  # Child windows are enumerated separately, never duplicated.
            stack.extend(reversed(list(node)))
            if node.tag != "node":
                continue
            raw_node_count += 1
            if node.get("visible-to-user") == "false":
                continue
            bounds = ANDROID_BOUNDS.fullmatch(node.get("bounds", ""))
            if not bounds:
                continue
            left, top, right, bottom = map(int, bounds.groups())
            if right <= left or bottom <= top:
                continue
            controls.append({**metadata, "resource_id": node.get("resource-id", ""),
                             "class_name": node.get("class", ""), "text": node.get("text", ""),
                             "content_desc": node.get("content-desc", ""),
                             "package": node.get("package", ""), "bounds": [left, top, right, bottom],
                             "enabled": node.get("enabled") == "true", "clickable": node.get("clickable") == "true"})
        is_navigation = metadata["window_type"] == "TYPE_SYSTEM" and metadata["window_title"] == "Navigation bar"
        windows.append({**metadata, "root_node_present_in_XML": bool(roots), "root_package": root_package,
                        "raw_node_count": raw_node_count, "controls": controls})
        if is_navigation:
            navigation.extend(controls)
        elif metadata["window_active"] or metadata["window_focused"]:
            active.extend(controls)
    return {"display_id": display_id, "controls": active, "navigation_controls": navigation,
            "windows": windows, "truncated": False}


def shared_policy(stage, **facts):
    """Check trusted inventory, launch data and broker observations. Never perform device I/O."""
    if stage == "runtime":
        device = facts["device"]
        if (device.get("status") != "running" or not facts["supported_endpoint"]
                or not SHA256.fullmatch(device.get("container_id") or "")
                or not INSTANCE_ID.fullmatch(device.get("instance_id") or "")
                or not RESOURCE_TAG.fullmatch(device.get("resource_tag") or "")):
            raise ValueError("The Android runtime is not eligible for a startup script")
    elif stage == "source":
        source, editing = facts["source"], facts.get("editing", False)
        if not isinstance(source, str) or not source or "\0" in source or (editing and not source.strip()):
            raise ValueError("Invalid script source")
        if editing:
            source = source.replace("\r\n", "\n")
        raw = source.encode("utf-8")
        return raw
    elif stage == "assignment":
        apk = facts["apk"]
        if not apk or not isinstance(apk.get("sha256"), str) or not SHA256.fullmatch(apk["sha256"]):
            raise ValueError("Invalid shared app assignment")
        size = apk.get("size")
        if type(size) is not int or size < 1:
            raise ValueError("Expected a positive integer APK size")
    elif stage == "launch":
        config = facts["config"]
        container = config.get("expected_container_id")
        if (config.get("shared_app") is not True or not config.get("instance_id")
                or not isinstance(container, str) or not SHA256.fullmatch(container)):
            raise ValueError("Invalid shared app assignment")
        size = config.get("assigned_apk_size")
        if config.get("assigned_apk"):
            shared_policy("assignment", apk={**config["assigned_apk"], "size": size})
        elif config.get("verify_apk", False):
            raise ValueError("APK verification requires an assigned APK")
        return {"shared_app": True, "expected_container_id": container, "assigned_apk_size": size}
    elif stage == "request":
        action, args = facts["action"], facts["args"]
        if action == "inspect-window" and len(args) <= 4:
            return "inspect"
        if action in APP_ACTIONS:
            shared_policy("app_action", action=action, args=args)
            return "app"
        if action == "adb":
            return "adb"
        return "action"
    elif stage == "app_action":
        action, args = facts["action"], facts["args"]
        one_argument = action in {"app-tap", "app-grant-permission"}
        if (len(args) != int(one_argument) or
                one_argument and (not isinstance(args[0], str) or not args[0] or "\0" in args[0])):
            raise ValueError("Invalid app test action arguments")
    elif stage in {"inspect_scope", "android_user", "package"}:
        # Package and Android-user choices are controlled by the script/panel.
        return None
    elif stage == "apk_candidates":
        paths = {match[1]: match[2] for row in facts["listing"].splitlines() if (match := APK_LIST_ROW.fullmatch(row))}
        if not paths:
            raise RuntimeError("Could not enumerate installed APKs in the shared app assignment")
        return paths
    elif stage == "apk_match":
        paths, matching = facts["paths"], facts["matching"]
        if len(matching) != 1 or matching[0] not in paths:
            raise RuntimeError("The APK installed through the panel was not found on this Android device")
        return paths[matching[0]]
    elif stage == "apk_path":
        path = APK_PATH.fullmatch(facts["output"])
        if not path:
            raise RuntimeError("The selected APK is not installed for the selected Android user")
        return path[1]
    elif stage == "apk_checksum":
        if facts["checksum"].split() != [facts["expected"], facts["path"]]:
            raise RuntimeError("The installed app differs from the uploaded APK selected for this test")
    elif stage == "selector":
        if facts["dialog"]:
            return "android:id/button1"
        resource_id = facts["resource_id"]
        if not isinstance(resource_id, str) or not resource_id or "\0" in resource_id:
            raise ValueError("Expected a resource ID")
        if ":id/" in resource_id:
            return resource_id
        return f"{facts['package']}:id/{resource_id}"
    elif stage == "focus":
        return bool(facts["focus"])
    elif stage == "stable_focus":
        return True
    elif stage == "control_nodes":
        root = facts["root"]
        if root.tag != "hierarchy":
            raise ValueError("Invalid Android UI hierarchy")
        nodes = list(reversed(root))
        controls = []
        while nodes:
            node = nodes.pop()
            # Traverse every subtree, including WebViews and editable controls.
            nodes.extend(reversed(node))
            if node.tag != "node" or node.get("visible-to-user") == "false":
                continue
            bounds = ANDROID_BOUNDS.fullmatch(node.get("bounds", ""))
            if bounds:
                left, top, right, bottom = map(int, bounds.groups())
                if right > left and bottom > top:
                    controls.append((node, (left, top, right, bottom)))
            if facts.get("limit") == len(controls):
                break
        return controls
    elif stage == "visible_controls":
        nodes = shared_policy("control_nodes", root=ElementTree.fromstring(facts["hierarchy"]))
        controls = [{
            "resource_id": node.get("resource-id", ""),
            "class_name": node.get("class", ""),
            "text": node.get("text", ""),
            "content_desc": node.get("content-desc", ""),
            "package": node.get("package", ""),
            "bounds": list(bounds),
            "enabled": node.get("enabled") == "true",
            "clickable": node.get("clickable") == "true",
        } for node, bounds in nodes]
        return controls, False
    elif stage == "window_snapshot":
        return window_snapshot(facts["hierarchy"], facts.get("display_id", 0))
    elif stage == "unique_control":
        matches = [bounds for node, bounds in shared_policy("control_nodes", root=facts["root"])
                   if node.get("resource-id") == facts["selector"]]
        return matches[0] if len(matches) == 1 else None
    elif stage == "tap_position":
        left, top, right, bottom = facts["bounds"]
        if right > left and bottom > top:
            return (left + right) // 2, (top + bottom) // 2
        return None
    elif stage == "native_tap":
        result = facts["result"]
        if not isinstance(result, dict) or result.get("ok") is not True:
            detail = result.get("error") if isinstance(result, dict) else None
            raise RuntimeError(f"Xenoid input tap was not confirmed: {str(detail or 'unknown response')[:160]}")
    else:
        raise ValueError(f"Unknown shared script policy stage: {stage}")
