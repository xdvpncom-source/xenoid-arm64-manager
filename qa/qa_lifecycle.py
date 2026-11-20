"""Interactions for the reviewed V2 VPN QA build on a single assigned Android user."""

import random
import re
import secrets
import shlex
import time
from xml.etree import ElementTree


PACKAGE = "v2.vpn.fast"
MAIN = PACKAGE + "/com.xdvpn.app.MainActivity"
GOOGLE_AD = PACKAGE + "/com.google.android.gms.ads.AdActivity"
APK_SHA256 = "c4eefd5af81351ca434984a46c3ad2eb49dd56ed9e5e5da1a74d86b58c781a50"
POWER_LABELS = {"connect", "disconnect", "اتصال", "قطع اتصال"}
CLOSE_LABELS = {"close", "بستن", "close ad", "interstitial close button"}
BUTTON_CLASSES = {"android.widget.Button", "android.widget.ImageButton"}
ANDROID_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
QA_ACTIONS = {"qa-open", "qa-dismiss", "qa-center", "qa-finish", "qa-dismiss-v2", "qa-center-v2",
              "qa-connect-check-v3", "qa-finish-v3", "qa-cycle-v4"}
VPN_DIALOG = "com.android.vpndialogs/.ConfirmDialog"
NOTIFICATION_ACTIVITY = "com.android.permissioncontroller/com.android.permissioncontroller.permission.ui.GrantPermissionsActivity"
LAUNCHER = "com.android.launcher3/com.android.launcher3.uioverrides.QuickstepLauncher"
DISCLOSURE = {"data collection disclosure", "شفافیت جمع‌آوری داده"}
AGREE = {"agree and continue", "موافقم و ادامه می‌دهم"}
GOOGLE_STARTUP_TEST_UNITS = {
    "interstitial": {"/21775744923/example/interstitial", "ca-app-pub-3940256099942544/1033173712"},
    "appOpen": {"/21775744923/example/app-open", "ca-app-pub-3940256099942544/9257395921"},
}


class CloseNotReady(RuntimeError):
    pass


def rect(node):
    match = ANDROID_BOUNDS.fullmatch(node.get("bounds", ""))
    return tuple(map(int, match.groups())) if match else None


def contains(bounds, point):
    return bounds and bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]


def labels(node):
    return {v.strip().casefold() for v in (node.get("text", ""), node.get("content-desc", "")) if v.strip()}


def match_control(root, component, action, size):
    nodes = list(root.iter("node"))
    if not nodes or component not in {MAIN, GOOGLE_AD}:
        raise RuntimeError("The QA app does not own the foreground window")
    screen_labels = set().union(*(labels(node) for node in nodes))
    if screen_labels & {"data collection disclosure", "شفافیت جمع‌آوری داده", "agree and continue",
                        "موافقم و ادامه می‌دهم", "update available", "آپدیت جدید آماده است"}:
        raise RuntimeError("A disclosure, consent or update dialog requires manual review")
    if action == "qa-dismiss" and component == MAIN:
        # Both the data disclosure (Close accepts it) and remotely configured sponsor
        # interstitials reside in MainActivity. Their generic Close labels are ambiguous.
        raise RuntimeError("No verified test-ad Close is visible in this app window")
    allowed_labels = CLOSE_LABELS if action == "qa-dismiss" else POWER_LABELS
    if component == GOOGLE_AD:
        if action != "qa-dismiss" or not any(
                node.get("package") == PACKAGE and labels(node) & {"test ad", "test ads", "test mode"}
                for node in nodes):
            raise RuntimeError("A verified Google test-ad close control is not visible")
    if action == "qa-center" and any(
            node.get("class") == "android.webkit.WebView" or labels(node) & CLOSE_LABELS for node in nodes):
        raise RuntimeError("A dialog or WebView is still visible above the app's power control")
    matches = []
    for node in nodes:
        if (node.get("package") == PACKAGE and node.get("class") in BUTTON_CLASSES
                and node.get("clickable") == "true" and node.get("enabled") == "true"
                and node.get("visible-to-user") != "false" and labels(node) & allowed_labels):
            matches.append(node)
    if len(matches) != 1:
        raise RuntimeError("Expected one visible enabled " + ("Close button" if action == "qa-dismiss" else "VPN power button")
                           + f"; found {len(matches)}")
    chosen = matches[0]
    box = rect(chosen)
    if not box or box[2] <= box[0] or box[3] <= box[1]:
        raise RuntimeError("The matched button has invalid bounds")
    if action == "qa-center":
        point = (size[0] // 2, size[1] // 2)
        if not contains(box, point):
            raise RuntimeError("The center of the display is outside the VPN power button")
    else:
        point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    if not contains((0, 0, *size), point):
        raise RuntimeError("The matched button is outside the display")
    if any(node is not chosen and node.get("clickable") == "true" and node.get("enabled") == "true"
           and contains(rect(node), point) for node in nodes):
        raise RuntimeError("Another control covers the requested touch position")
    return point


def available(node, package):
    return (node.get("package") == package and node.get("enabled") == "true"
            and node.get("visible-to-user") != "false")


def agreement_button(root, size):
    nodes = [n for n in root.iter("node") if available(n, PACKAGE)]
    words = {line.strip().casefold() for n in nodes for value in labels(n) for line in value.splitlines()}
    identity = (DISCLOSURE, AGREE, {"terms of service", "شرایط استفاده"}, {"privacy policy", "سیاست حریم خصوصی"},
                {"v2 vpn uses the system vpn service to provide a secure and reliable connection.",
                 "v2 vpn از سرویس vpn سیستم برای ارائه اتصال امن و پایدار استفاده می‌کند."})
    if not all(words & choices for choices in identity) or any(n.get("class") == "android.webkit.WebView" for n in nodes):
        raise RuntimeError("The first-party V2 data collection disclosure is not verified")
    buttons = [n for n in nodes if n.get("class") in BUTTON_CLASSES and n.get("clickable") == "true"]
    agree = [n for n in buttons if labels(n) & AGREE]
    if len(agree) != 1:
        raise RuntimeError("No unique first-party V2 disclosure acceptance control")
    named = [n for n in buttons if labels(n) & CLOSE_LABELS]
    if len(named) > 1:
        raise RuntimeError("Ambiguous Close controls in the first-party disclosure")
    if not named:
        titles = [n for n in nodes if labels(n) & DISCLOSURE and rect(n)]
        if len(titles) != 1:
            raise RuntimeError("The V2 disclosure title cannot be located")
        title_top = rect(titles[0])[1]
        # This build's Flutter top row has one 48dp square icon button before the
        # title and no tooltip. It sits on the leading edge of the bottom sheet.
        rtl = "شفافیت جمع‌آوری داده" in words
        for n in buttons:
            box = rect(n)
            if not box or labels(n):
                continue
            left,top,right,bottom = box
            w,h = right-left, bottom-top
            leading = left > size[0]*.75 if rtl else right < size[0]*.25
            if leading and 0 < w < size[0]*.2 and .86*w <= h <= 1.15*w and 0 <= top < bottom <= title_top:
                named.append(n)
    if len(named) != 1:
        raise RuntimeError("The V2 disclosure Close icon cannot be distinguished; no agreement was sent")
    chosen = named[0]
    box = rect(chosen)
    if not box or box[2] <= box[0] or box[3] <= box[1]:
        raise RuntimeError("The first-party disclosure control has invalid bounds")
    point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    if (not contains((0, 0, *size), point)
            or any(n is not chosen and n.get("clickable") == "true" and contains(rect(n), point) for n in nodes)):
        raise RuntimeError("The first-party disclosure control is obscured")
    return "V2 disclosure Close (app records agreement)", point


def demo_close(root, size):
    # This installed debug build pins Google SDK interstitial placements to its
    # test units. Its custom remote promotions are confined to MainActivity.
    nodes = list(root.iter("node"))
    parents = {child: parent for parent in root.iter() for child in parent}
    close_labels = CLOSE_LABELS | {"dismiss ad", "dismiss advertisement", "close advertisement",
                                 "close button", "بستن تبلیغ", "بستن آگهی"}
    close_symbols = {"×", "✕", "✖"}
    close_ids = {"ad_close_button", "close_ad_button", "interstitial_close_button"}
    def lineage(node):
        path = [node]
        while path[-1] in parents:
            path.append(parents[path[-1]])
        return path[::-1]
    def visible(n):
        return n.get("visible-to-user") != "false"
    def web(n):
        return n.get("class") == "android.webkit.WebView"
    def named(n):
        package, sep, resource = n.get("resource-id", "").partition(":id/")
        words = labels(n)
        return bool(words & close_labels or (words and words <= close_symbols) or
                    (sep and package in {PACKAGE, "com.google.android.gms.ads"} and resource in close_ids))
    candidates = []
    for n in nodes:
        if (not available(n, PACKAGE) or not named(n)
                or n.get("class") not in BUTTON_CLASSES | {"android.widget.ImageView"}
                or any(web(ancestor) for ancestor in lineage(n))):
            continue
        parent = parents[n]
        # Some SDK versions delegate touch to a containing native frame.
        delegated = (available(parent, PACKAGE) and parent.get("class") == "android.widget.FrameLayout"
                     and parent.get("clickable") == "true" and [c for c in parent if visible(c)] == [n]
                     and not (labels(parent) - close_labels - close_symbols))
        if n.get("clickable") == "true" or delegated:
            candidates.append((n, parent if delegated else None))
    if not candidates:
        raise CloseNotReady("Google's test AdActivity has not rendered a native Close control")
    if len(candidates) != 1:
        raise RuntimeError("Google's test AdActivity does not contain one unique native Close control")
    target, delegate = candidates[0]
    box = rect(target)
    if not box or box[0] < 0 or box[1] < 0 or box[2] > size[0] or box[3] > size[1]:
        raise RuntimeError("The Google SDK Close bounds cannot be verified on this display")
    point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    if not contains(box,point) or delegate is not None and not contains(rect(delegate),point):
        raise RuntimeError("The Google SDK Close has no available touch target")
    ancestry = lineage(target)
    def underneath(node):
        # A native close is often drawn over a full-screen WebView. Verify that
        # the native branch follows the isolated WebView branch in their common
        # layout's recorded child order. Never treat another overlay as that WebView.
        path = lineage(node)
        i = next((i for i,(a,b) in enumerate(zip(path,ancestry)) if a is not b), None)
        if i is None or i == 0:
            return False
        common, background, overlay = path[i-1],path[i],ancestry[i]
        if common.get("class") not in {"android.widget.RelativeLayout", "android.widget.FrameLayout"}:
            return False
        if not (background is node or any(web(n) for n in path[i:])):
            return False
        branch = background
        while not web(branch):
            children = [c for c in branch if visible(c)]
            if branch.get("class") != "android.widget.FrameLayout" or len(children) != 1:
                return False
            branch = children[0]
        if overlay is not target and overlay is not delegate:
            if overlay.get("class") != "android.widget.FrameLayout" or list(overlay) != [target]:
                return False
        key = "drawing-order" if "drawing-order" in background.attrib or "drawing-order" in overlay.attrib else "index"
        back, front = background.get(key, ""), overlay.get(key, "")
        return back.isdigit() and front.isdigit() and int(back) < int(front) and (
            key == "drawing-order" or list(common).index(background) < list(common).index(overlay))
    for n in nodes:
        if not visible(n) or not contains(rect(n), point) or n is target or n is delegate:
            continue
        if n in ancestry and n.get("clickable") != "true":
            continue
        if not underneath(n):
            raise RuntimeError("Another view covers the native Google SDK Close target")
    return "verified Google SDK test ad Close", point


HOME_STATES = {
    "connected": {"connected", "متصل"},
    "connecting": {"connecting", "در حال اتصال"},
    "disconnected": {"disconnected", "قطع", "قطع شده", "قطع شده است"},
    "disconnecting": {"disconnecting", "در حال قطع"},
}


def home_state(root):
    nodes = [n for n in root.iter("node") if available(n, PACKAGE)]
    words = {line.strip() for n in nodes for text in labels(n) for line in text.splitlines()}
    if ("v2 vpn" not in words or not words & {"selected server", "سرور انتخاب‌شده", "سرور انتخاب شده"}
            or words & (DISCLOSURE | AGREE)):
        raise RuntimeError("Cannot verify the V2 home screen")
    states = []
    for n in nodes:
        parts = {line.strip() for text in labels(n) for line in text.splitlines()}
        if not any(re.fullmatch(r"(?:.*[,،]\s*)?\d+:\d{2}:\d{2}", line) for line in parts):
            continue
        names = {re.split(r"[,،]", line, maxsplit=1)[0].strip() for line in parts}
        states.extend(state for state, aliases in HOME_STATES.items() if aliases & names)
    if len(states) != 1:
        raise RuntimeError("The V2 connection status and its timer are ambiguous")
    return states[0]


def home_power(root, size, *, expected="disconnected", center_on_button=False):
    if expected not in {"disconnected","connecting"} or home_state(root) != expected:
        raise RuntimeError("The V2 power button is not in the expected state")
    nodes = list(root.iter("node"))
    action_labels = {"connect", "اتصال"} if expected == "disconnected" else {"disconnect", "قطع اتصال"}
    parents = [n for n in nodes if available(n, PACKAGE) and n.get("class") == "android.widget.Button"
               and labels(n) & action_labels]
    if len(parents) != 1:
        raise RuntimeError("Cannot identify V2's unique Connect control")
    parent = parents[0]
    point = (size[0]//2,size[1]//2)
    children = list(parent)
    if center_on_button and parent.get("clickable") == "true" and not any(
            n is not parent and n.get("clickable") == "true" for n in parent.iter("node")):
        target = parent
    else:
        # The indeterminate Flutter progress indicator has no numeric value.
        if (parent.get("clickable") != "false" or len(children) != 1 or not available(children[0], PACKAGE)
                or children[0].get("class") != "android.widget.Button" or children[0].get("clickable") != "true"
                or (labels(children[0]) != {"0"} and not (expected == "connecting" and not labels(children[0])))):
            raise RuntimeError("V2's verified power-button gesture target is not present")
        target = children[0]
    box = rect(target)
    if center_on_button and box:
        point = ((box[0]+box[2])//2,(box[1]+box[3])//2)
    if not contains(rect(parent),point) or not contains((0,0,*size),point) or not contains(box,point):
        raise RuntimeError("The screen center is outside V2's power-button gesture target")
    w,h = box[2]-box[0],box[3]-box[1]
    if not .9*w <= h <= 1.1*w or ((2*point[0]-box[0]-box[2])/w)**2 + ((2*point[1]-box[1]-box[3])/h)**2 >= 1:
        raise RuntimeError("The screen center is outside the circular power-button hit area")
    if any(n is not target and contains(rect(n),point) and (n.get("clickable") == "true"
           or n.get("class") in {"android.webkit.WebView","android.widget.ImageView"}) for n in nodes):
        raise RuntimeError("Another view covers the V2 power-button touch")
    return point


def vpn_record(dump, uid):
    """Read only current connected VPN networks with a default route on the primary link."""
    match = re.search(r"(?m)^Current Networks:[ \t]*$", dump)
    if not match:
        return None
    section = re.split(r"(?m)^\S", dump[match.end():], maxsplit=1)[0]
    owned = []
    for line in section.splitlines():
        if not line.lstrip().startswith("NetworkAgentInfo{network{"):
            continue
        transport = re.search(r"Transports:\s*([^]]*?)\s+Capabilities:", line)
        owner = re.search(r"\bOwnerUid:\s*(\d+)\b", line)
        link = re.search(r"lp\{\{InterfaceName:\s*([a-zA-Z0-9_.-]{1,15})\s", line)
        routes = re.search(r"Routes:\s*\[([^]]*)]", line)
        if (not transport or "VPN" not in re.findall(r"\w+",transport[1]) or not owner or
                owner[1] != str(uid) or not link or not routes or
                not re.search(r"\bni\{VPN[^}]*?\bCONNECTED\b", line)):
            continue
        iface = link[1]
        for route in routes[1].split(","):
            if (re.search(r"^\s*(?:0\.0\.0\.0/0|::/0)\s+->", route)
                    and re.search(r"(?:^|\s)" + re.escape(iface) + r"(?:\s|$)", route)
                    and not re.search(r"unreachable|throw|prohibit",route,re.I)):
                handle = re.search(r"\bhandle\{(\d+)\}",line)
                owned.append((iface, int(handle[1]) if handle else None))
                break
    return owned[0] if len(owned) == 1 else None


def vpn_network(dump, uid):
    network = vpn_record(dump, uid)
    return network[0] if network else None


def vpn_request_owner(activities, uid):
    # Bind this particular resumed ConfirmDialog to Android's recorded launching
    # UID, package and user. Some Android 13 dumps omit the optional resultTo.
    vpn = r"com\.android\.vpndialogs/(?:com\.android\.vpndialogs\.)?\.?ConfirmDialog"
    marker = r"ActivityRecord\{([0-9a-fA-F]+) u0 " + vpn + r"\}? t(\d+)\b"
    resumed = set(re.findall(r"(?:topResumedActivity|mResumedActivity)\s*[:=]\s*" + marker, activities))
    if len(resumed) != 1:
        raise RuntimeError("Android did not identify exactly one resumed VPN confirmation")
    token, task = resumed.pop()
    hist = re.split(r"(?m)^[ \t]*\*?\s*Hist\s+#\d+:\s+", activities)[1:]
    for section in hist:
        header, _, rest = section.partition("\n")
        match = re.match(marker, header)
        if not match or match.groups() != (token, task):
            continue
        launched = re.findall(r"(?m)^[ \t]*launchedFromUid=(\d+) launchedFromPackage=(\S+)([^\n]*)$", rest)
        if len(launched) != 1 or launched[0][:2] != (str(uid), PACKAGE):
            break
        result = re.findall(r"(?m)^[ \t]*resultTo=(.*)$", rest)
        target = (len(result) == 1 and re.match(r"ActivityRecord\{[0-9a-fA-F]+ u0 "
                  + re.escape(MAIN) + r"\}? t\d+\b", result[0]))
        user = re.search(r"\buserId=(\d+)\b", launched[0][2])
        if (target or not result) and ((user and user[1] == "0") or (not user and target)):
            return
        break
    raise RuntimeError("The resumed VPN confirmation is not verified as a request from V2 VPN")


def vpn_button(root, size, *, cancel=False):
    package = "com.android.vpndialogs"
    nodes = [n for n in root.iter("node") if available(n, package)]
    if not any(n.get("resource-id") == package + ":id/warning"
               and any(re.search(r"\bv2 vpn\b", value, re.I) for value in labels(n)) for n in nodes):
        raise RuntimeError("The Android VPN dialog does not name V2 VPN as requester")
    control = "android:id/button2" if cancel else "android:id/button1"
    buttons = [n for n in nodes if n.get("class") == "android.widget.Button"
               and n.get("resource-id") == control and n.get("clickable") == "true"]
    if cancel and (len(buttons) != 1 or not labels(buttons[0]) & {"cancel", "لغو", "انصراف"}):
        return None
    if len(buttons) != 1 or not rect(buttons[0]):
        raise RuntimeError("Android's system VPN approval button is missing")
    box = rect(buttons[0])
    point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    if (not contains(box, point) or not contains((0,0,*size),point) or any(
            n is not buttons[0] and n.get("clickable") == "true" and contains(rect(n),point) for n in nodes)):
        raise RuntimeError("Android's VPN approval button is obscured")
    return point


def recent_tasks(dump):
    if "ACTIVITY MANAGER RECENT TASKS" not in dump:
        raise RuntimeError("Android did not return its recent task list")
    # AOSP Task.toString() has both compact and cached verbose forms. The compact
    # header omits U= entirely. Read the task's own dump before later summaries.
    chunks = re.split(r"(?m)^[ \t]*\* Recent #\d+: ", dump)[1:]
    result = []
    for chunk in chunks:
        header, _, rest = chunk.partition("\n")
        lines = []
        for line in rest.splitlines():
            if line and not line.startswith("    "):
                break
            lines.append(line)
        body = "\n".join(lines)
        owner = re.search(r"(?m)^    mActivityComponent=([^/\s]+)/", body)
        relevant = owner[1] == PACKAGE if owner else bool(re.search(
            r"(?<![\w.])" + re.escape(PACKAGE) + r"(?![\w.])", header))
        if not relevant:
            continue
        header_user = re.search(r"\bU=(\d+)\b", header)
        body_user = re.search(r"(?m)^    userId=(\d+)\b", body)
        users = {v[1] for v in (header_user, body_user) if v}
        if len(users) != 1:
            raise RuntimeError("Cannot verify the Android user owning the QA task")
        if users != {"0"}:
            continue
        if not owner:
            raise RuntimeError("Cannot identify the package owning its recent task")
        task = re.match(r"Task\{[^{}\n]*?\s#(\d+)\b", header)
        if not task:
            raise RuntimeError("Cannot resolve the QA app's Android task number")
        result.append(int(task[1]))
    return result


class QaLifecycle:
    def __init__(self, broker, assigned, repeat_count, canary, *, window_actions=False):
        self.broker = broker
        self.assigned = assigned
        self.repeat_count = repeat_count
        self.canary = canary
        self.window_actions = window_actions
        self.window_offer = None
        self.window_delivered = set()
        self.last_public_home = None
        self.step = "new"
        self.opened_epoch = None
        self.notification_sent = False
        self.disclosure_sent = False

    def shell(self, *args, timeout=12):
        return self.broker.adb(["shell", shlex.join(args)], timeout=timeout)

    def verify(self):
        if not self.assigned or self.assigned["sha256"] != APK_SHA256:
            raise RuntimeError("This device has no assignment for the reviewed V2 VPN QA APK")
        if self.shell("am", "get-current-user") != "0":
            raise RuntimeError("The QA lifecycle is scoped to Android user 0")
        paths = self.shell("pm", "path", "--user", "0", PACKAGE).splitlines()
        if len(paths) != 1 or not re.fullmatch(r"package:/data/app/[A-Za-z0-9_+=~/.-]+/base\.apk", paths[0]):
            raise RuntimeError("The expected standalone QA APK is not installed for user 0")
        path = paths[0][len("package:"):]
        checksum = self.shell("sha256sum", path, timeout=20)
        if checksum.split() != [APK_SHA256, path]:
            raise RuntimeError("Installed V2 VPN does not match the reviewed APK")
        version = self.shell("pm", "list", "packages", "--show-versioncode", "--user", "0", PACKAGE)
        if "package:" + PACKAGE + " versionCode:46" not in version.splitlines():
            raise RuntimeError("The assigned Android package version changed")

    def window(self):
        windows = self.shell("dumpsys", "window")
        found = re.findall(r"(?m)^\s*mCurrentFocus=Window\{\S+ u0 ([^\s}]+)\}", windows)
        if len(found) != 1:
            raise RuntimeError("Cannot identify a single foreground window for user 0")
        return VPN_DIALOG if found[0] == "com.android.vpndialogs/com.android.vpndialogs.ConfirmDialog" else found[0]

    def screen(self):
        root = ElementTree.fromstring(self.broker.dispatch("ui-dump", []))
        sizes = re.findall(r"(?m)^(?:Physical|Override) size: (\d+)x(\d+)\s*$", self.shell("wm", "size"))
        if not sizes or root.get("rotation") not in {"0", "1", "2", "3"}:
            raise RuntimeError("Cannot verify display orientation and dimensions")
        size = tuple(map(int, sizes[-1]))
        return root, size[::-1] if root.get("rotation") in {"1", "3"} else size

    def scoped_windows(self):
        return ((self.canary or self.window_actions) and self.repeat_count == 1
                and self.assigned and self.assigned.get("sha256") == APK_SHA256)

    def inspect_window(self):
        watcher = QaWatcher(self)
        component, token = watcher.focus()
        if component == LAUNCHER:
            return "launcher", component, token, {}
        unknown = ("unknown", component or "", token, {})
        if not self.scoped_windows() or component not in (MAIN, GOOGLE_AD, VPN_DIALOG, NOTIFICATION_ACTIVITY):
            return unknown
        try:
            self.verify()
            choices = {}
            surface, point = watcher.observe(component, public_choices=choices)
            kind = {"Google test ad": "test-ad", "V2 disclosure": "disclosure", "home": "home",
                    "V2 VPN permission": "vpn", "V2 notification prompt": "notification",
                    "V2 startup splash": "splash"}[surface]
            if kind == "test-ad" and point is not None:
                choices["close"] = ("Close Google test ad", point)
            elif kind == "disclosure":
                choices["close"] = ("Close V2 disclosure (records agreement)", point)
            elif kind == "vpn":
                choices = {"allow": ("Allow V2 VPN connection", point), **choices}
            elif kind == "home":
                state, power, _ = point
                if state == "disconnected":
                    choices["connect"] = ("Connect V2 VPN", power)
        except (RuntimeError, ElementTree.ParseError):
            return unknown
        current, current_token = watcher.focus()
        if (current, current_token) != (component, token):
            return "unknown", current or "", current_token, {}
        return kind, component, token, choices

    def detect_window(self):
        # The broker serializes requests for this exact registered device. Even
        # an observation with no actions supersedes the previous public offer.
        self.window_offer = None
        kind, component, token, choices = self.inspect_window()
        key = (kind, token)
        if kind == "home":
            if self.last_public_home == token:
                self.window_delivered = {item for item in self.window_delivered
                                         if item[0] not in {"test-ad", "disclosure"}}
            if "connect" not in choices:
                self.window_delivered.discard(key)
            self.last_public_home = token
        else:
            self.last_public_home = None
        if key in self.window_delivered:
            choices = {}
        nonce = None
        if choices:
            nonce = secrets.token_urlsafe(24)
            self.window_offer = (nonce, time.monotonic() + 90, key, component, choices)
        return {"kind": kind, "package": component.partition("/")[0] if "/" in component else "",
                "component": component, "observation": nonce,
                "actions": [{"name": name, "label": label} for name, (label, _) in choices.items()]}

    def handle_window(self, observation, action):
        def result(handled, reason):
            return {"handled": handled, "action": action, "reason": reason}

        offer = self.window_offer
        if not offer or not isinstance(observation, str) or observation != offer[0]:
            return result(False, "Observation is stale or has no available actions")
        # Consume before validation or transport. Delivery can be ambiguous
        # after an error, and a different gesture must require a new lookup.
        self.window_offer = None
        _, expires, key, component, choices = offer
        if not isinstance(action, str) or action not in choices:
            return result(False, "That action was not offered")
        if time.monotonic() >= expires or not self.scoped_windows():
            return result(False, "Observation expired or the device assignment changed")
        kind, current, token, fresh = self.inspect_window()
        if (time.monotonic() >= expires or current != component or (kind, token) != key
                or fresh.get(action) != choices[action]):
            return result(False, "The observed window or control changed")
        watcher = QaWatcher(self)
        self.window_delivered.add(key)
        if not watcher.send_focused(component, token, fresh[action][1]):
            self.window_delivered.discard(key)
            return result(False, "Android focus changed before dispatch")
        self.broker.log("action", f"Sent Xenoid position click for {kind}/{action}")
        return result(True, "Xenoid accepted the position click")

    def inspect_control(self, action):
        component = self.window()
        if component not in {MAIN, GOOGLE_AD}:
            raise RuntimeError("The active window is outside the assigned app or a system dialog is open")
        root, size = self.screen()
        if action == "qa-dismiss-v2":
            if component == MAIN:
                label, point = agreement_button(root, size)
            else:
                self.verify_test_placement()
                label, point = demo_close(root, size)
        elif action == "qa-center-v2":
            if component != MAIN:
                raise RuntimeError("V2's own MainActivity is not in the foreground")
            label, point = "verified V2 power control", home_power(root, size)
        else:
            point = match_control(root, component, action, size)
            label = "explicit Close button" if action == "qa-dismiss" else "verified VPN power control"
        return component, label, point

    def verify_test_placement(self):
        pid = self.shell("pidof", PACKAGE)
        if not pid.isdigit() or int(pid) == 0 or self.opened_epoch is None:
            raise RuntimeError("Cannot identify the active V2 process that requested the test ad")
        # Return only the latest startup policy marker from this process; omit
        # ad traffic and all other application messages from the broker and logs.
        command = (f"logcat -b main -d --pid={int(pid)} -v epoch -s flutter:I "
                   "| grep -F '[JamJamStartup] policy ' | tail -n 1")
        last = self.broker.adb(["shell", command], timeout=12)
        when = re.match(r"^\s*(\d+)\.\d+\s", last)
        slot = re.search(r"\[JamJamStartup\] policy enabled=true format=(\w+) unit=(\S+)(?:\s|$)", last)
        if (not when or int(when[1]) < self.opened_epoch or not slot
                or slot[2] not in GOOGLE_STARTUP_TEST_UNITS.get(slot[1], ())):
            raise RuntimeError("V2 has not reported a matching Google SDK startup test unit for this launch")

    def tap(self, action):
        sdk = action == "qa-dismiss-v2" and self.window() == GOOGLE_AD
        first = None
        deadline = time.monotonic() + 35
        for _ in range(4 if sdk else 2):
            if sdk and (time.monotonic() > deadline or self.window() != GOOGLE_AD):
                raise RuntimeError("The Google SDK window changed or its native Close did not settle")
            try:
                current = self.inspect_control(action)
            except CloseNotReady:
                if not sdk:
                    raise
                first = None
                time.sleep(.4)
                continue
            if sdk and current[0] != GOOGLE_AD:
                raise RuntimeError("The foreground changed while locating Google's test Close")
            if first == current and self.window() == current[0]:
                break
            first = current
        else:
            raise RuntimeError("The control or foreground window moved before the touch")
        x, y = current[2]
        self.step = "touching"
        self.shell("input", "tap", str(x), str(y))
        return current[0], current[1]

    def dismiss_optional_notification(self):
        if self.notification_sent:
            raise RuntimeError("The optional notification prompt reappeared; no second Back was sent")
        for _ in range(2):
            if self.window() != NOTIFICATION_ACTIVITY:
                raise RuntimeError("The Android permission request changed before BACK")
            root, _ = self.screen()
            pkg = "com.android.permissioncontroller"
            prompts = [n for n in root.iter("node") if available(n, pkg)
                       and n.get("resource-id") == pkg + ":id/permission_message"]
            if len(prompts) != 1 or prompts[0].get("text") != "Allow V2 VPN to send you notifications?":
                raise RuntimeError("The Android permission request is not V2's optional notification prompt")
        if self.window() != NOTIFICATION_ACTIVITY:
            raise RuntimeError("The notification prompt changed before BACK")
        self.notification_sent = True
        self.shell("input", "keyevent", "KEYCODE_BACK")
        for _ in range(6):
            time.sleep(.3)
            if self.window() != NOTIFICATION_ACTIVITY:
                self.broker.log("action", "Dismissed V2's optional notification prompt with Back")
                return
        raise RuntimeError("The optional notification prompt remained after Back")

    def await_main(self):
        notification_seen = False
        for _ in range(10):
            window = self.window()
            if window == MAIN:
                return
            if window == NOTIFICATION_ACTIVITY and not notification_seen:
                notification_seen = True
                self.dismiss_optional_notification()
            elif window != GOOGLE_AD:
                raise RuntimeError("Returning to V2 VPN encountered another window; no center touch was sent")
            time.sleep(.5)
        raise RuntimeError("The Google demo Close did not return to the V2 app; no center touch was sent")

    def observe_first_party_control(self):
        component = self.window()
        if component != MAIN:
            return component, None, None
        root, size = self.screen()
        component = self.window()
        if component != MAIN:
            return component, None, None
        words = {line.strip().casefold() for n in root.iter("node") if available(n, PACKAGE)
                 for value in labels(n) for line in value.splitlines()}
        if words & DISCLOSURE:
            label, point = agreement_button(root, size)
            return MAIN, label, point
        return MAIN, "V2 Connect", home_power(root, size)

    def prepare_vpn(self):
        # MainActivity can be focused briefly while Android schedules its optional
        # notification request. Discard both lookups whenever focus changes.
        self.step = "preparing-v2"
        previous = None
        for _ in range(10):
            selection = self.observe_first_party_control()
            component, label, point = selection
            if component == NOTIFICATION_ACTIVITY:
                self.dismiss_optional_notification()
                previous = None
                continue
            if component != MAIN:
                raise RuntimeError("V2 foreground changed to an unrecognized window; input stopped")
            if previous != selection:
                previous = selection
                continue
            if self.disclosure_sent and label != "V2 Connect":
                # Wait for Flutter to render home; never tap the same X twice.
                time.sleep(.3)
                continue
            # Inspect focus again inside the very same Android shell command as
            # input. Device identity checks and ADB round trips are outside the
            # observation-to-injection interval here.
            if not self.tap_home(point):
                previous = None
                continue
            if label == "V2 Connect":
                self.broker.log("action", "Tapped the display center within the verified VPN power button")
                return
            self.disclosure_sent = True
            self.broker.log("action", "Tapped " + label)
            previous = None
            time.sleep(.3)
        raise RuntimeError("The V2 foreground did not settle on its verified Connect button")

    def tap_home(self, point):
        match = r"^[[:space:]]*mCurrentFocus=Window\{[^ ]+ u0 v2\.vpn\.fast/com\.xdvpn\.app\.MainActivity\}$"
        x, y = point
        command = (f"if dumpsys window | grep -Eq {shlex.quote(match)}; then "
                   f"input tap {x} {y} && echo QA_TAPPED; else echo QA_FOCUS_CHANGED; fi")
        reply = self.broker.adb(["shell", command], timeout=12)
        if reply == "QA_FOCUS_CHANGED":
            return False
        if reply != "QA_TAPPED":
            raise RuntimeError("Android did not confirm the V2 touch; it was not repeated")
        return True

    def connection_view(self):
        if self.window() != MAIN:
            raise RuntimeError("Post-permission check requires V2's own home screen")
        root, size = self.screen()
        if self.window() != MAIN:
            raise RuntimeError("Foreground changed during the VPN check")
        state = home_state(root)
        point = home_power(root, size, expected=state, center_on_button=True) if state in ("disconnected","connecting") else None
        errors = any(re.search(r"\b(?:tls|http|dns)\s+fail\b", value)
                     for n in root.iter("node") if available(n, PACKAGE) for value in labels(n))
        return state, point, errors

    def network_interface(self):
        packages = self.shell("pm", "list", "packages", "-U", "--user", "0", PACKAGE)
        ids = re.findall(r"(?m)^package:" + re.escape(PACKAGE) + r" uid:(\d+)$", packages)
        if len(ids) != 1 or not 10000 <= int(ids[0]) < 100000:
            raise RuntimeError("Cannot verify the VPN application UID")
        dump = self.shell("dumpsys", "connectivity", timeout=15)
        if "Current Networks:" not in dump:
            raise RuntimeError("Android did not return the list of current networks")
        self.observed_network = vpn_record(dump, ids[0])
        return self.observed_network[0] if self.observed_network else None

    def https_ok(self, iface):
        from qa_vpn_probe import https_204
        record = self.observed_network
        if not record or record[0] != iface or not record[1]:
            return False
        ok = https_204(self.shell, record[1])
        return bool(ok and self.network_interface() == iface and self.observed_network == record)

    def check_connection(self, watcher=None):
        if not self.permission_mode():
            raise RuntimeError("Android has not authorized V2 VPN for the active user")
        retried = cancelled = False
        last = None
        fail_seen = False
        start = time.monotonic()
        deadline = start + 105
        while time.monotonic() < deadline:
            if watcher:
                before = time.monotonic()
                observation = watcher.poll()
                if observation is None:
                    # Superseded observations must never count towards a stable double tap.
                    last = None
                    deadline += time.monotonic() - before + 1
                    time.sleep(1)
                    continue
                state, point, failed_probe = observation
            else:
                state, point, failed_probe = self.connection_view()
            iface = self.network_interface()
            now = time.monotonic()
            view = (state, point, bool(iface))
            if view != last or failed_probe and not fail_seen:
                note = " (candidate probe failed)" if failed_probe else ""
                self.broker.log("action", f"Post-permission state: {state}; owned Android VPN: {iface or 'none'}{note}")
            if state == "connected" and iface:
                if self.https_ok(iface):
                    return {"connected": True, "detail": "App Connected, matching Android VPN route, and HTTPS 204 through its network", "retried": retried}
                return {"connected": False, "detail": "V2 reported Connected but HTTPS 204 through its VPN network could not be verified", "retried": retried}
            if not retried and view == last and not iface and point:
                ready = state == "disconnected"
                stalled = state == "connecting" and failed_probe and fail_seen and now-start >= 35 and not cancelled
                if ready or stalled:
                    if watcher:
                        if not watcher.tap_power(point):
                            last = None
                            continue
                    elif self.tap_home(point) is False:
                        raise RuntimeError("The V2 home screen changed before the retry touch")
                    cancelled = cancelled or stalled
                    retried = ready
                    self.broker.log("action", "Tapped Connect again after VPN permission" if ready else
                                    "Cancelled stalled V2 connection attempt after repeated probe failures")
                    last = None
                    fail_seen = False
                    deadline = now + (105 if ready else 55)
                    time.sleep(2)
                    continue
            last = view
            fail_seen = fail_seen or failed_probe
            time.sleep(3)
        state = last[0] if last else "unknown"
        reason = f"Connection unverified within the observation window: app {state}"
        if fail_seen:
            reason += ", candidate TLS/HTTP/DNS probe failures observed"
        if last and not last[2]:
            reason += ", no routed Android VPN owned by V2"
        return {"connected": False, "detail": reason, "retried": retried}

    def permission_mode(self):
        reply = self.shell("cmd", "appops", "get", "--user", "0", PACKAGE, "ACTIVATE_VPN")
        return bool(re.search(r"(?m)^ACTIVATE_VPN:\s*allow\b", reply))

    def verify_vpn_caller(self):
        packages = self.shell("pm", "list", "packages", "-U", "--user", "0", PACKAGE)
        uid = re.findall(r"(?m)^package:" + re.escape(PACKAGE) + r" uid:(\d+)$", packages)
        if len(uid) != 1 or not 10000 <= int(uid[0]) < 100000:
            raise RuntimeError("Cannot verify V2 VPN application UID for user 0")
        vpn_request_owner(self.shell("dumpsys", "activity", "-a", "activities"), uid[0])

    def inspect_vpn(self):
        if self.window() != VPN_DIALOG:
            raise RuntimeError("Android's VPN confirmation left the foreground")
        self.verify_vpn_caller()
        root, size = self.screen()
        return vpn_button(root, size)

    def authorize_vpn(self):
        for _ in range(16):
            window = self.window()
            if window == VPN_DIALOG:
                first = self.inspect_vpn()
                current = self.inspect_vpn()
                if current != first or self.window() != VPN_DIALOG:
                    raise RuntimeError("Android's VPN confirmation changed before approval")
                self.shell("input", "tap", str(current[0]), str(current[1]))
                for _ in range(6):
                    if self.permission_mode():
                        self.broker.log("action", "Android confirmed ACTIVATE_VPN for V2 VPN, user 0")
                        return
                    time.sleep(.4)
                raise RuntimeError("Android did not confirm VPN authorization for this package")
            if self.permission_mode():
                self.broker.log("action", "Android already authorizes V2 VPN for user 0")
                return
            if window != MAIN:
                raise RuntimeError("Waiting for V2 VPN permission reached an unrelated system or app window")
            time.sleep(.4)
        raise RuntimeError("V2 VPN did not present its Android VPN approval after the power touch")

    def finish(self):
        for task in self.tasks():
            if task not in self.tasks():
                continue
            reply = self.shell("am", "stack", "remove", str(task))
            if "error" in reply.casefold() or "exception" in reply.casefold():
                raise RuntimeError("Android refused to dismiss the QA task")
        for attempt in range(6):
            if not self.tasks():
                break
            if attempt == 5:
                raise RuntimeError("The QA app remains in Android Recents; data was preserved")
            time.sleep(0.3)
        self.broker.log("action", "Verified removal of V2 VPN from Android Recents")
        self.verify()
        if self.shell("pm", "clear", "--user", "0", PACKAGE, timeout=25) != "Success":
            raise RuntimeError("Android did not confirm clearing this QA app's data")
        if self.tasks():
            raise RuntimeError("A QA task reappeared after Android cleared app data")
        self.broker.log("action", "Android confirmed V2 VPN data cleared for user 0")

    def tasks(self):
        return recent_tasks(self.shell("dumpsys", "activity", "-a", "recents"))

    def cycle_v4(self):
        self.dispatch("qa-open", [])
        self.step = "watching"
        watcher = QaWatcher(self)
        wait = random.randint(90, 110)
        self.broker.log("action", f"Watching for known dialogs during the initial wait of {wait} seconds")
        watcher.dwell("initial wait", wait)
        watcher.phase = "first Connect"
        previous = None
        while True:
            view = watcher.poll()
            if view is None:
                previous = None
            else:
                state, point, _ = view
                if state in {"connecting", "connected"}:
                    break
                if state == "disconnected" and previous == (watcher.home_token, state, point):
                    if watcher.tap_power(point):
                        self.broker.log("action", "Tapped the display center within the verified VPN power button")
                        break
                previous = (watcher.home_token, state, point)
            time.sleep(.6)
        watcher.phase = "VPN permission"
        # A permission prompt can predate the first Connect tap or surface after
        # an intervening ad. Only Android's app-op confirms the grant.
        permission_deadline = time.monotonic() + 75
        while not self.permission_mode():
            before = time.monotonic()
            if watcher.poll() is None:
                permission_deadline += time.monotonic() - before
            if time.monotonic() >= permission_deadline:
                raise RuntimeError("V2 VPN did not receive Android VPN permission")
            time.sleep(.6)
        if not watcher.reported_grant:
            self.broker.log("action", "Android already authorizes V2 VPN for user 0")
        watcher.phase = "connection check"
        result = self.check_connection(watcher)
        self.broker.log("action", "VPN check: " + result["detail"])
        self.broker.log("action", "Watching for known dialogs during the final wait of 30 seconds")
        watcher.dwell("final wait", 30)
        self.step = "finishing"
        self.verify()
        self.finish()
        self.step = "finished"
        return result

    def dispatch(self, action, args):
        self.window_offer = None
        if not self.canary or self.repeat_count != 1:
            raise RuntimeError("This online V2 VPN QA build requires one assigned canary device and a single diagnostic round; use an isolated staging build for repeated tests")
        if args:
            raise ValueError("The reviewed QA actions accept no package, URL or coordinates")
        expected = {"qa-open": "new", "qa-dismiss": "opened", "qa-dismiss-v2": "opened",
                    "qa-center": "dismissed", "qa-center-v2": "dismissed-v2", "qa-finish": "tapped",
                    "qa-connect-check-v3": "tapped", "qa-finish-v3": "vpn-checked", "qa-cycle-v4": "new"}
        if self.step != expected[action]:
            raise RuntimeError(f"{action} is not valid in the current QA step ({self.step})")
        if action == "qa-cycle-v4":
            return self.cycle_v4()
        self.verify()
        if action == "qa-open":
            # The Android action may complete even if the transport subsequently times out.
            self.step = "opening"
            clock = self.shell("date", "+%s")
            self.opened_epoch = int(clock) if clock.isdigit() else None
            response = self.shell("am", "start", "-W", "--user", "0", "-a", "android.intent.action.MAIN", "-c",
                                  "android.intent.category.LAUNCHER", "-n", MAIN, timeout=25)
            if not re.search(r"(?m)^Status:\s*ok\s*$", response) or "Error:" in response:
                raise RuntimeError("Android did not confirm opening the V2 VPN QA build")
            self.step = "opened"
            self.broker.log("action", "Opened verified V2 VPN QA build v46")
        elif action == "qa-dismiss-v2":
            if self.window() == NOTIFICATION_ACTIVITY:
                self.dismiss_optional_notification()
            component, label = self.tap(action)
            self.broker.log("action", "Tapped " + label)
            if component == GOOGLE_AD:
                self.await_main()
            else:
                self.disclosure_sent = True
            self.step = "dismissed-v2"
        elif action == "qa-center-v2":
            self.prepare_vpn()
            self.authorize_vpn()
            self.step = "tapped"
        elif action == "qa-connect-check-v3":
            self.step = "checking-vpn"
            result = self.check_connection()
            self.broker.log("action", "VPN check: " + result["detail"])
            self.step = "vpn-checked"
            return result
        elif action in {"qa-dismiss", "qa-center"}:
            self.tap(action)
            self.step = "dismissed" if action == "qa-dismiss" else "tapped"
            self.broker.log("action", "Tapped the explicit Close button" if action == "qa-dismiss"
                            else "Tapped the display center within the verified VPN power button")
        else:
            self.step = "finishing"
            self.finish()
            self.step = "finished"
        return self.step


class QaWatcher:
    """Serialize recognition, focus checks and input on the assigned QA device."""

    def __init__(self, driver):
        self.driver = driver
        self.phase = "initial wait"
        self.initial_until = time.monotonic()
        self.expires = time.monotonic() + 720
        self.previous = None
        self.sent = set()
        self.home_token = None
        self.last_surface = None
        self.surface_since = None
        self.blocked_since = None
        self.reported_grant = False
        self.saw_ad = False

    def focus(self):
        windows = self.driver.shell("dumpsys", "window")
        found = re.findall(r"(?m)^\s*mCurrentFocus=Window\{([0-9A-Fa-f]+) u0 ([^\s}]+)\}", windows)
        if len(found) != 1:
            return None, None
        token, component = found[0]
        if component == "com.android.vpndialogs/com.android.vpndialogs.ConfirmDialog":
            component = VPN_DIALOG
        return component, token

    def send(self, component, token, point):
        self.driver.verify()
        return self.send_focused(component, token, point)

    def send_focused(self, component, token, point):
        if component not in (MAIN, GOOGLE_AD, NOTIFICATION_ACTIVITY, VPN_DIALOG) or not re.fullmatch("[a-fA-F0-9]+", token):
            raise RuntimeError("Cannot bind the QA gesture to a specific Android window")
        if point is not None:
            if self.focus() != (component, token):
                return False
            self.driver.broker.official_tap(*point)
            return True
        if component != NOTIFICATION_ACTIVITY:
            raise RuntimeError("Back is only available for V2's optional notification prompt")
        guard = r"^[[:space:]]*mCurrentFocus=Window\{" + token + " u0 " + re.escape(component) + r"\}$"
        cmd = (f"if dumpsys window | grep -Eq {shlex.quote(guard)}; then "
               "input keyevent KEYCODE_BACK && echo QA_TAPPED; else echo QA_FOCUS_CHANGED; fi")
        reply = self.driver.broker.adb(["shell", cmd], timeout=12)
        if reply == "QA_FOCUS_CHANGED":
            return False
        if reply != "QA_TAPPED":
            raise RuntimeError("Android did not confirm the QA gesture; no second input was sent")
        return True

    def tap_power(self, point):
        if not self.home_token or self.focus() != (MAIN, self.home_token):
            self.previous = None
            return False
        return self.send(MAIN, self.home_token, point)

    def test_ad(self, root):
        # In this signed debug APK, _unitId() returns Google's Android demo ID
        # for all regular bc/ac/dc placements before reading remotely supplied
        # IDs. Startup uses a separate withTestAdUnit() path. Require additional
        # evidence in the current SDK window or from this app process.
        words = {x for n in root.iter("node") if available(n, PACKAGE) for x in labels(n)}
        demo = ("you're displaying a test interstitial from google ad manager." in words
                or {"you're displaying a test interstitial from", "google ad manager"} <= words)
        if demo:
            return
        try:
            self.driver.verify_test_placement()
            return
        except RuntimeError:
            pass
        pid = self.driver.shell("pidof", PACKAGE)
        if not pid.isdigit() or not self.driver.opened_epoch:
            raise RuntimeError("No launch-scoped Google SDK test placement is available")
        cmd = (f"logcat -b main -d --pid={int(pid)} -v epoch -s flutter:I "
               "| grep -E 'AdMob (bc|ac|dc): .*unit=' | tail -n 1")
        row = self.driver.broker.adb(["shell", cmd], timeout=12)
        marker = re.match(r"^\s*(\d+)\.\d+\s.*AdMob (?:bc|ac|dc): .*unit=([^ ]+)", row)
        if (not marker or int(marker[1]) < self.driver.opened_epoch
                or marker[2].strip() != "ca-app-pub-3940256099942544/1033173712"):
            raise RuntimeError("The visible Google SDK ad could not be identified as a test placement")

    def observe(self, component, *, public_choices=None):
        if component not in {MAIN, GOOGLE_AD, NOTIFICATION_ACTIVITY, VPN_DIALOG}:
            raise RuntimeError("An unrecognized Android window is in the foreground")
        if component == VPN_DIALOG:
            self.driver.verify_vpn_caller()
        root, size = self.driver.screen()
        if component == GOOGLE_AD:
            self.test_ad(root)
            try:
                _, point = demo_close(root, size)
            except RuntimeError:
                if public_choices is None:
                    raise
                # The SDK surface is verified even before its native Close
                # is rendered or if the target is obscured.
                point = None
            return "Google test ad", point
        if component == VPN_DIALOG:
            allow = vpn_button(root, size)
            if public_choices is not None:
                try:
                    cancel = vpn_button(root, size, cancel=True)
                except RuntimeError:
                    cancel = None
                if cancel is not None:
                    public_choices["cancel"] = ("Cancel V2 VPN connection request", cancel)
            return "V2 VPN permission", allow
        if component == NOTIFICATION_ACTIVITY:
            pkg = "com.android.permissioncontroller"
            prompts = [n for n in root.iter("node") if available(n, pkg)
                       and n.get("resource-id") == pkg + ":id/permission_message"]
            if len(prompts) != 1 or prompts[0].get("text") != "Allow V2 VPN to send you notifications?":
                raise RuntimeError("The system permission prompt is not V2's notification request")
            return "V2 notification prompt", None
        words = {line.strip() for n in root.iter("node") if available(n, PACKAGE)
                 for text in labels(n) for line in text.splitlines()}
        if ("v2 vpn" in words and words & {"preparing the best route", "در حال آماده‌سازی بهترین مسیر"}
                and words & {"loading servers", "در حال دریافت سرورها"}
                and not any(n.get("class") == "android.webkit.WebView" for n in root.iter("node"))):
            return "V2 startup splash", None
        if words & DISCLOSURE:
            _, point = agreement_button(root, size)
            return "V2 disclosure", point
        state = home_state(root)
        point = (home_power(root, size, expected=state, center_on_button=self.phase != "first Connect")
                 if state in {"disconnected","connecting"} else None)
        failed = any(re.search(r"\b(?:tls|http|dns)\s+fail\b", x) for x in words)
        return "home", (state, point, failed)

    def blocked(self, reason):
        self.previous = None
        self.home_token = None
        if self.blocked_since is None:
            self.blocked_since = time.monotonic()
            self.driver.broker.log("action", f"Waiting during {self.phase}: {reason}")
        if time.monotonic() - self.blocked_since >= 45:
            raise RuntimeError(f"Could not safely resume {self.phase}: {reason}")

    def poll(self):
        if time.monotonic() >= self.expires:
            raise RuntimeError("The single-device QA cycle reached its time limit")
        component, token = self.focus()
        try:
            kind, choice = self.observe(component)
        except (RuntimeError, ElementTree.ParseError) as error:
            # Only observation functions are retried. Transport uncertainty after
            # an input is not recoverable by replaying the gesture.
            self.blocked(str(error))
            return None
        if self.focus() != (component, token):
            self.blocked("Android focus changed during inspection")
            return None
        if kind == "V2 startup splash":
            # This signed application can remain on its own loading screen for
            # the configured startup budget even if no Google ad is delivered.
            self.home_token = self.previous = None
            if self.phase == "initial wait" and time.monotonic() <= self.initial_until:
                self.blocked_since = None
            else:
                self.blocked("V2 is still loading")
            return None
        if kind == "home":
            # A sheet within MainActivity can recur with the same token. Two
            # independent Home observations establish that the earlier modal
            # really left before accepting a later presentation of it.
            if self.home_token == token and self.last_surface == ("home", token):
                self.sent = {key for key in self.sent if key[0] not in {"V2 disclosure", "Google test ad"}}
            self.home_token = token
            self.previous = None
            self.blocked_since = self.surface_since = None
            self.last_surface = ("home", token)
            return choice
        self.home_token = None
        key = (kind, token)
        if self.last_surface != key:
            self.driver.broker.log("action", f"Observed {kind} during {self.phase}")
            self.last_surface = key
            self.surface_since = time.monotonic()
        if self.surface_since is not None and time.monotonic() - self.surface_since >= 75:
            raise RuntimeError(f"The {kind} did not settle or leave during {self.phase}")
        if key in self.sent:
            self.blocked(f"Waiting for the handled {kind} to leave the foreground")
            return None
        identity = (kind, token, choice)
        if self.previous != identity:
            self.previous = identity
            return None
        self.previous = None
        # A second stable lookup succeeded. An explicit focus mismatch is the
        # only case in which input can be attempted on a later observation.
        if not self.send(component, token, choice):
            self.blocked("Android focus changed before the gesture")
            return None
        self.sent.add(key)
        self.blocked_since = None
        if kind == "V2 VPN permission":
            for _ in range(6):
                if self.driver.permission_mode():
                    self.reported_grant = True
                    self.driver.broker.log("action", "Android confirmed ACTIVATE_VPN for V2 VPN, user 0")
                    return None
                time.sleep(.4)
            raise RuntimeError("Android did not confirm VPN authorization after the verified approval")
        if kind == "Google test ad":
            self.saw_ad = True
            message = "Tapped verified Google SDK test ad Close"
        elif kind == "V2 disclosure":
            message = "Tapped V2 disclosure Close (app records agreement)"
        else:
            message = "Dismissed V2's optional notification prompt with Back"
        self.driver.broker.log("action", message)
        return None

    def dwell(self, phase, seconds):
        self.phase = phase
        until = time.monotonic() + seconds
        if phase == "initial wait":
            self.initial_until = until
        while True:
            home = self.poll()
            if time.monotonic() >= until and home is not None:
                if phase == "initial wait" and not self.saw_ad:
                    self.driver.broker.log("action", "Initial wait finished on V2 Home without a test ad; continuing")
                return
            time.sleep(.7)
