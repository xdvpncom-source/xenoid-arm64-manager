"""One VPN QA cycle; the panel repeats this file while repeat_count is zero.

Each device resolves the APK assigned to it by the panel. Devices without an
assignment are not queued. No app is selected from the current foreground window.
"""

import math
import hashlib
import json
import random
import re
import time
import uuid
from xenoid_device import Device

RUN_SETTINGS = {
    "repeat_count": 0, "initial_delay": 0, "timeout": 0,
    "command_timeout": 60, "verify_apk": True,
}
# With both DISPLAY values None, inherit the central panel resource/profile
# settings. Explicit values retain the earlier per-script override behavior:
# "minimum" uses the values below; 1 restores native; numeric scales use Physical.
DISPLAY_SCALE = None                  # None inherits current panel/profile dimensions.
MIN_DISPLAY_SIDE = 200
MIN_DISPLAY_DPI = 72
DISPLAY_FPS = None                    # None inherits panel FPS; 0 explicitly restores native FPS.
DISPLAY_SETUP_TIMEOUT = 180            # Initial native FPS setup may restart Android graphics.
START_WAIT_SECONDS = (90, 120)          # Random dwell after VPN permission, before ad dismissal.
COLD_START_APP = True                  # Close a surviving app/VPN session before each launch; preserve app data.
AFTER_AD_WAIT_SECONDS = 10             # Foreground hold after ad dismissal; handle new popups.
CONNECTED_WAIT_SECONDS = 0             # Optional extra seconds added to the post-permission dwell.
POLL_SECONDS = 2
UI_INSPECTION_PRIORITY = 0             # None uses the runtime's default UI reader priority.
UI_ALL_WINDOWS = True                 # Read real WebView Close nodes and observed navigation controls.
CRASH_POLL_SECONDS = 5
CRASH_FALLBACK_SECONDS = 30
# Idle waits use cheap focus reads between full UI inspections. Full reads still
# happen periodically: an in-app overlay may not change Android's focused window.
IDLE_FOCUS_POLL_SECONDS = 5
IDLE_UI_MIN_SECONDS = 2
IDLE_UI_MAX_SECONDS = 10
ACTION_COOLDOWN_SECONDS = 2
AD_READY_SECONDS = 180
AD_FINAL_READY_VERIFY_SECONDS = 30    # One read-only confirmation if the app becomes clear at the deadline.
AD_PROGRESS_WAIT_SECONDS = 60          # Unknown progress budget; 0 disables this cutoff.
AD_PROGRESS_STALL_SECONDS = 30         # Unchanged countdown/progress; 0 disables this cutoff.
AD_COUNTDOWN_TOP_FRACTION = .35         # Unlabelled countdowns are only recognized above the ad.
AD_LIST_CLICKABLE_CONTROLS = True       # List observed controls when the ad UI changes.
AD_CONTROL_LOG_INTERVAL_SECONDS = 10   # Changed inventories may be logged this often; 0 logs every change.
DIALOG_READY_SECONDS = 240
CONNECT_READY_SECONDS = 180
VPN_READY_SECONDS = 180
POST_ACTION_QUIET_SECONDS = 2
CONTINUE_AFTER_CYCLE_ERROR = True
FAILED_CYCLE_BACKOFF_SECONDS = 15
RETRY_EXIT_CODE = 75
DISMISS_ANDROID_CRASH_DIALOGS = True
# Draw once after a successful app cycle and cleanup. A matching draw runs the
# official Xenoid identity regeneration, including its Android soft restart.
REGENERATE_ENABLED = True
REGENERATE_RANDOM_RANGE = (1, 10)
REGENERATE_TRIGGER = 5
REGENERATE_TIMEOUT = 0                 # 0 waits without a setup deadline.
REGENERATE_RETRY_SECONDS = 15
# Clear only these packages' CACHE after a committed native regeneration.
# An empty tuple disables this phase. Add "com.android.vending" only if needed.
GOOGLE_CACHE_PACKAGES = ("com.google.android.gms", "com.google.android.gsf")
GOOGLE_CACHE_USER = "current"
GOOGLE_CACHE_TIMEOUT = 0               # 0 waits without a cache-clear deadline.
GOOGLE_CACHE_RETRY_SECONDS = 15
GOOGLE_DATA_RESET_ENABLED = True
GOOGLE_DATA_PACKAGES = ("com.google.android.gms", "com.google.android.gsf")
GOOGLE_RESET_DELAY_SECONDS = 2         # Cache -> data clear -> microG registration; also between package clears.
GOOGLE_RESET_TIMEOUT = 0               # 0 permits retries without an operation deadline.

# Explicit resource IDs or labels can be added here for a particular APK.
# Example: ({"resource_id": "com.example.vpn:id/ad_close"},)
AD_CLOSE_SELECTORS = ()
TERMS_CLOSE_SELECTORS = ()
CONNECT_SELECTORS = ()
CONNECT_POSITION = None               # Optional explicit (x, y); otherwise an observed Connect control is required.
TAP_BACKEND = "native"                 # Retained setting; this scenario accepts official native input only.
GEOMETRIC_AD_CLOSE = False             # An anonymous corner can be an ad click-through, not Close.
TERMS_BACK_FALLBACK = False            # Legacy setting; use an observed close control, never an ADB key.
AD_LANDING_PACKAGES = ("com.android.vending",)  # Explicit destinations allowed after a recorded ad-close tap.
AD_LANDING_REQUIRE_OBSERVED_AD = False # After the completed dwell, allow one Back from configured stores even if a fast redirect was missed.
AD_LANDING_RETURN_LIMIT = 1            # Tap only an observed native Android Back control.
AD_LANDING_ORIGIN_BACK_LIMIT = 1       # After store Back, dismiss only the same originating ad.
AD_MISSING_CLOSE_BACK_SECONDS = 15     # Grace after the initial dwell when a real AdActivity exposes no Close.
AD_MISSING_CLOSE_BACK_LIMIT = 1        # Per semantic ad state, using only an observed native Back control.
AD_NAV_SNAPSHOT_MAX_SECONDS = 5       # Client receipt age; broker independently checks its one-shot offer.
AD_RECOVERY_VERIFY_SECONDS = 60       # One post-Back verification allowance; 0 adds no allowance.
AD_LANDING_PROVENANCE_SECONDS = 120    # A store already foreground before an ad click is not recovered.
AD_SAME_TARGET_ATTEMPT_LIMIT = 2        # Do not keep clicking an unchanged accessibility target.
AD_ACTIVITY_HINTS = ("AdActivity", "Interstitial", "RewardedAd", "RewardedVideo", "AdUnitActivity")
AD_ACTIVITY_NAMESPACE_PREFIXES = (
    "com.google.android.gms.ads.", "com.applovin.", "com.unity3d.services.ads.",
    "com.mbridge.", "com.bytedance.sdk.openadsdk.", "com.ironsource.", "com.vungle.", "com.chartboost.",
)

CLOSE_WORDS = {"close", "close ad", "close advertisement", "close button", "dismiss",
               "dismiss ad", "skip ad", "بستن", "بستن تبلیغ", "بستن آگهی", "رد کردن تبلیغ"}
CLOSE_IDS = {"close", "close_button", "ad_close", "ad_close_button", "close_ad_button",
             "interstitial_close_button", "close_ad", "closebutton", "skip_ad", "skip_button"}
# Some WebView accessibility IDs do not preserve word separators or camel case.
# Match complete IDs only; arbitrary substrings such as "disclose" are not Close.
AD_PACKED_CLOSE_IDS = {"adclose", "adclosebutton", "closead", "closeadbutton", "closebtn",
                       "dismissbtn", "dismissbutton", "mraidclose", "mraidclosebutton"}
CLOSE_SYMBOLS = {"×", "✕", "✖", "x"}
CLOSE_ACTION_TOKENS = {"close", "dismiss", "skip", "بستن", "ببند", "ببندید"}
CLOSE_LABEL_TOKENS = CLOSE_ACTION_TOKENS | {
    "ad", "ads", "advert", "advertisement", "advertisements", "advertising", "interstitial",
    "video", "rewarded", "reward", "fullscreen", "full", "screen", "end", "card", "endcard",
    "this", "the", "current", "a", "an", "to", "tap", "click", "press", "please", "now",
    "button", "btn", "icon", "image", "view", "popup", "pop", "up", "dialog", "window",
    "admob", "google", "gms", "unity", "unityads", "applovin", "mbridge", "pangle",
    "تبلیغ", "تبلیغات", "آگهی", "این", "را", "کردن", "شدن", "رد", "عبور", "گذشتن", "از", "دکمه",
}
CLOSE_REJECT_TOKENS = {
    "buy", "purchase", "shop", "install", "download", "get", "learn", "more", "open", "visit",
    "claim", "subscribe", "register", "login", "account", "delete", "share", "rate",
    "closed",
    "mute", "unmute", "sound", "audio", "volume", "play", "pause", "replay", "report", "feedback",
    "not", "no", "never", "cannot", "cant", "dont", "disabled", "unavailable", "locked",
    "wait", "waiting", "soon", "remaining", "after",
    "خرید", "نصب", "دانلود", "دریافت", "بیشتر", "صدا", "بیصدا",
    "نبند", "نبندید", "نشود", "نمی", "نیست", "غیرفعال", "انتظار", "بعد", "پس", "مانده",
}
AD_CLOSE_INFO_TOKENS = {"adchoices", "choices", "choice", "info", "information", "about", "privacy", "policy",
                        "disclosure", "اطلاعات", "حریم"}
GEOMETRIC_CLOSE_LABELS = {"button", "imagebutton", "image button", "image", "icon", "view"}
TERMS_WORDS = {"terms of service", "terms and conditions", "privacy policy", "data collection disclosure",
               "قوانین", "شرایط استفاده", "سیاست حریم خصوصی", "شفافیت جمع آوری داده"}
CONNECT_WORDS = {"connect", "اتصال", "وصل شدن"}
LOADING_DIALOG_WORDS = {"refreshing server list", "please wait while the app fetches enough servers",
                        "almost ready to connect"}
NOTIFICATION_PACKAGES = {"com.android.permissioncontroller", "com.google.android.permissioncontroller",
                         "com.android.packageinstaller", "com.google.android.packageinstaller"}
VPN_PACKAGE = "com.android.vpndialogs"
CRASH_CLOSE_IDS = {"android:id/aerr_close", "android:id/aerr_close_button"}
CRASH_WORDS = {"keeps stopping", "has stopped", "isn't responding", "is not responding",
               "متوقف شده", "متوقف می شود", "پاسخ نمی دهد"}
CRASH_CLOSE_WORDS = {"close app", "force close", "بستن برنامه", "توقف اجباری"}
SYSTEM_DIALOG_PACKAGES = {"android", "com.android.systemui"}
CRASH_FOCUS_COMMAND = ["shell", "dumpsys window displays | grep -E 'mCurrentFocus=|mFocusedApp='"]
CRASH_FOCUS_HINTS = {"application error", "application not responding", "apperrordialog",
                     "appnotrespondingdialog", "keeps stopping", "has stopped"}


def normalized(value):
    return " ".join(str(value).replace("\u200c", " ").split()).casefold()


def labels(control):
    return {normalized(control.get(key, "")) for key in ("text", "content_desc")} - {""}


def close_tokens(value):
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", str(value))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = normalized(value.replace("'", "").replace("’", "")).replace("ي", "ی").replace("ك", "ک")
    return set(re.findall(r"[^\W\d_]+|\d+", value, re.UNICODE))


def close_rejected(control, *, ad=False):
    values = [control.get(key, "") for key in ("text", "content_desc")]
    tokens = set().union(*(close_tokens(value) for value in values))
    if ad:
        tokens |= close_tokens(control.get("resource_id", "").rsplit("/", 1)[-1])
    # A close-like ID cannot override a CTA, negation or an unfinished countdown.
    return (bool(tokens & (CLOSE_REJECT_TOKENS | (AD_CLOSE_INFO_TOKENS if ad else set())))
            or any(int(number) > 0 for value in values for number in re.findall(r"\d+", str(value))))


def semantic_close_label(control):
    for field in ("text", "content_desc"):
        tokens = close_tokens(control.get(field, ""))
        action = bool(tokens & CLOSE_ACTION_TOKENS) or (
            bool(tokens & {"رد", "عبور", "گذشتن"}) and bool(tokens & {"تبلیغ", "تبلیغات", "آگهی"}))
        if action and all(token in CLOSE_LABEL_TOKENS or token.isdecimal() and int(token) == 0 for token in tokens):
            return True
    return False


def bounds(control):
    box = control.get("bounds")
    if (isinstance(box, (tuple, list)) and len(box) == 4
            and all(isinstance(value, (int, float)) for value in box)
            and box[2] > box[0] and box[3] > box[1]):
        return tuple(box)
    return None


def center(box):
    return int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2)


def matches(control, selector):
    for key, value in selector.items():
        actual = control.get(key, "")
        if key == "resource_id" and ":" not in value:
            actual = str(actual).rsplit(":id/", 1)[-1]
        if key in {"text", "content_desc"}:
            actual, value = normalized(actual), normalized(value)
        if actual != value:
            return False
    return True


def choose_unique(scored):
    if not scored:
        return None
    highest = max(score for score, _ in scored)
    unique = {bounds(control): control for score, control in scored if score == highest}
    unique.pop(None, None)
    return next(iter(unique.values())) if len(unique) == 1 else None


def choose_ad_close(scored):
    """Collapse nested accessibility representations of one observed button.

    No geometric corner guessing: every candidate already has Close semantics.
    Distinct buttons, windows, equal-size overlaps and anonymous parents remain
    ambiguous. A chosen actionable point must lie in every candidate rectangle.
    """
    selected = choose_unique(scored)
    if selected is not None or not scored:
        return selected
    highest = max(score for score, _ in scored)
    candidates = [control for score, control in scored if score == highest]
    identities = {(control.get("package"), control.get("window_id"), control.get("display_id"))
                  for control in candidates}
    if (len(identities) != 1 or any(type(control.get("window_id")) is not int
                                  or type(control.get("display_id")) is not int
                                  for control in candidates)):
        return None
    boxes = {bounds(control) for control in candidates}
    nested = [control for control in candidates if control.get("clickable") is True
              and all(box[0] <= bounds(control)[0] < bounds(control)[2] <= box[2]
                      and box[1] <= bounds(control)[1] < bounds(control)[3] <= box[3]
                      for box in boxes)]
    return choose_unique([(1, control) for control in nested])


def controls(window):
    return [control for control in window.get("controls", ())
            if control.get("enabled", False) and bounds(control)]


def application_hierarchy_missing(window):
    """A stable WM focus is not proof of an active app accessibility window."""
    proof = window.get("focus_observation")
    if isinstance(proof, dict) and proof.get("schema") == "xenoid.wm-focus-observation/v1":
        return proof.get("hierarchy_state") in {"partial_application_window_missing", "partial_application_root_missing",
                                                "partial_application_owner_mismatch"}
    # Preserve old single-hierarchy consumers; a nonempty all-window result can
    # still explicitly prove that it contains no active application window.
    windows = window.get("windows")
    return bool(windows) and not any(item.get("window_type") == "TYPE_APPLICATION" and
        (item.get("window_active") is True or item.get("window_focused") is True) for item in windows)


def explicit_control(window, selectors):
    for selector in selectors:
        selected = choose_unique([(1, control) for control in controls(window) if matches(control, selector)])
        if selected:
            return selected
    return None


def text_contains(window, needles):
    return any(needle in value for control in window.get("controls", ())
               for value in labels(control) for needle in needles)


def is_crash_dialog(window):
    if any(control.get("resource_id", "") in CRASH_CLOSE_IDS
           for control in window.get("controls", ())):
        return True
    # Text fallback is limited to a platform crash window or a named Bluetooth crash.
    # A webpage mentioning a crash must not be treated as Android's error dialog.
    crash_text = text_contains(window, CRASH_WORDS)
    platform = window.get("package") in SYSTEM_DIALOG_PACKAGES | {"com.android.bluetooth"}
    bluetooth = text_contains(window, {"bluetooth", "بلوتوث"})
    native_controls = any(control.get("resource_id", "").startswith("android:id/")
                          for control in window.get("controls", ()))
    return crash_text and (platform or (bluetooth and native_controls)) and text_contains(window, CRASH_CLOSE_WORDS)


def ump_consent_groups(window):
    """Recognize the observed consent sheet, never a general privacy footer."""
    if window.get("stable") is not True:
        return ()
    groups = {}
    for control in window.get("controls", ()):
        if (not bounds(control) or control.get("window_active") is False or control.get("window_focused") is False
                or control.get("package", window.get("package")) != window.get("package")):
            continue
        key = (control.get("package", window.get("package")),
               control.get("window_id"), control.get("display_id"))
        groups.setdefault(key, []).append(control)
    matches = []
    for items in groups.values():
        observed = set().union(*(labels(control) for control in items))
        title = any("asks for your consent to use your personal data" in value for value in observed)
        if title and {"consent", "do not consent", "manage options"} <= observed:
            matches.append(items)
    return matches


def ump_consent_controls(window):
    groups = ump_consent_groups(window)
    return groups[0] if len(groups) == 1 else ()


def is_ump_consent(window):
    return bool(ump_consent_groups(window))


def ump_decline_control(window):
    items = ump_consent_controls(window)
    context = [control for control in items if labels(control) & {"consent", "do not consent", "manage options"}
               or any("asks for your consent to use your personal data" in value for value in labels(control))]
    if not context or any(type(control.get("window_id")) is not int or control["window_id"] < 0
                          or type(control.get("display_id")) is not int or control["display_id"] < 0
                          or control.get("window_active") is not True or control.get("window_focused") is not True
                          for control in context):
        return None
    return choose_unique([(1, control) for control in items
                          if control.get("enabled") is True and labels(control) == {"do not consent"}
                          and (control.get("clickable") is True
                               or control.get("class_name", "").rsplit(".", 1)[-1] == "Button")])


def is_terms(window):
    if is_ump_consent(window):
        return True
    strong = {"data collection disclosure", "شفافیت جمع آوری داده"}
    if text_contains(window, strong):
        return True
    # Permanent privacy/terms footer links are not a modal consent dialog.
    consent = {"agree and continue", "i agree", "accept terms", "موافقم و ادامه می دهم", "می پذیرم"}
    return text_contains(window, TERMS_WORDS) and text_contains(window, consent)


def is_notification(window):
    return (window.get("package") in NOTIFICATION_PACKAGES
            and text_contains(window, {"notification", "notifications", "اعلان", "نوتیفیکیشن"}))


def is_ad_activity(window):
    component = str(window.get("component", ""))
    activity = component.partition("/")[2] or component
    words = close_tokens(activity.rsplit(".", 1)[-1])
    if any(close_tokens(hint) <= words for hint in AD_ACTIVITY_HINTS):
        return True
    # Fullscreen/video alone is also used by ordinary media apps. Require a
    # configured ad namespace before treating those broader names as an ad.
    sdk = activity.casefold().startswith(AD_ACTIVITY_NAMESPACE_PREFIXES)
    return sdk and "activity" in words and (
        bool(words & {"fullscreen", "video", "ad", "reward", "rewarded"}) or {"full", "screen"} <= words)


def is_ad(window):
    if is_ump_consent(window):
        return False
    return (is_ad_activity(window)
            or text_contains(window, {"advertisement", "sponsored", "test ad", "تبلیغ"}))


def is_loading(window, package):
    if is_ump_consent(window):
        return False
    return (window.get("package") == package
            and text_contains(window, {normalized(word) for word in LOADING_DIALOG_WORDS if normalized(word)}))


def is_blocking_ad(window, package):
    """An inline banner is not an obstruction to a visible Connect control."""
    if not is_ad(window):
        return False
    if is_ad_activity(window) or window.get("package") != package:
        return True
    selected = explicit_control(window, CONNECT_SELECTORS)
    if selected is None:
        selected = choose_unique([(1, control) for control in controls(window)
                                  if control.get("package", package) == package
                                  and labels(control) & CONNECT_WORDS])
    if selected is None:
        if (not isinstance(CONNECT_POSITION, (tuple, list)) or len(CONNECT_POSITION) != 2
                or any(type(value) is not int for value in CONNECT_POSITION)):
            return True
        point = CONNECT_POSITION
        selected_bounds = None
    else:
        point = center(bounds(selected))
        selected_bounds = bounds(selected)
    for control in controls(window):
        left, top, right, bottom = bounds(control)
        covers_connect = left <= point[0] < right and top <= point[1] < bottom
        if not covers_connect or bounds(control) == selected_bounds:
            continue
        words = labels(control)
        ad_label = any(needle in word for needle in {"advertisement", "sponsored", "test ad", "تبلیغ"}
                       for word in words)
        foreign = control.get("package") not in (None, "", package)
        if ad_label or "WebView" in control.get("class_name", "") or foreign:
            return True
    return False


class Cycle:
    def __init__(self, device, package):
        self.device, self.package = device, package
        self.user = device.settings().get("android_user")
        self.user = "current" if self.user is None else str(self.user)
        self.last_action = float("-inf")
        self.last_observation_error = None
        self.latest_observation_window = None
        self.last_snapshot_record = None
        self.last_read_stats = None
        self.missing_close_reason = "not_observed"
        self.missing_close_grace_seconds = 0.0
        self.last_navigation_result = None
        self.last_navigation_reported_reason = None
        self.last_permission_error = None
        self.last_crash_error = None
        self.last_crash_probe_error = None
        self.last_crash_fallback = float("-inf")
        self.target_crash = None
        self.home_component = None
        self.home_checked = False
        self.invalidate_idle_monitor()
        self.ad_close_attempts = {}
        self.ad_unsafe_targets = set()
        self.last_ad_close = None
        self.ad_landing_returns = 0
        self.ad_landing_origin = None
        self.missing_ad_close = None
        self.missing_ad_close_backs = {}
        self.last_ad_window = None
        self.post_connect_dwell_active = False
        self.post_connect_dwell_complete = False
        self.observed_post_connect_ad = None
        self.ad_progress_wait = None
        self.ad_progress_blocked = False
        self.last_ad_controls_digest = None
        self.last_ad_controls_at = float("-inf")

    def log(self, message):
        self.device.log(message)

    def observe(self):
        started = time.monotonic()
        self.latest_observation_window = None
        try:
            options = {"windows": True} if UI_ALL_WINDOWS else {}
            if UI_INSPECTION_PRIORITY is not None:
                options["priority"] = UI_INSPECTION_PRIORITY
            window = self.device.inspect_window(**options)
            finished = time.monotonic()
            self.last_read_stats = {"rpc_seconds": max(0.0, finished - started),
                                    "stable_focus": window.get("stable") is True,
                                    "application_hierarchy_missing": application_hierarchy_missing(window)}
            self.last_observation_error = None
            if window.get("stable"):
                self.latest_observation_window = window
                self.last_snapshot_record = window, finished, dict(self.last_read_stats)
                return window
            return None
        except (RuntimeError, OSError) as error:
            self.last_read_stats = {"rpc_seconds": max(0.0, time.monotonic() - started),
                                    "stable_focus": False, "error_class": type(error).__name__}
            detail = str(error)
            if detail != self.last_observation_error:
                self.log("Waiting for readable Android UI: " + detail)
                self.last_observation_error = detail
            return None

    def paired_focus(self, window):
        proof = window.get("focus_observation")
        if (window.get("stable") is not True or not isinstance(proof, dict)
                or proof.get("schema") != "xenoid.wm-focus-observation/v1"
                or proof.get("exact_match") is not True or proof.get("geometry_match") is not True
                or type(proof.get("display_id")) is not int or proof["display_id"] != window.get("display_id")
                or proof.get("package") != window.get("package")
                or not isinstance(proof.get("component"), str) or not isinstance(window.get("component"), str)
                or self.canonical_component(proof.get("component", "")) != self.canonical_component(window.get("component", ""))
                or not isinstance(proof.get("token"), str) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", proof["token"])
                or type(proof.get("user")) is not int or proof["user"] < 0):
            return None
        elapsed = proof.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            return None
        return proof

    def tap_observed_navigation(self, window, reason):
        """Choose a fresh one-shot native action; never fall back to coordinates or an ADB key."""
        proof = self.paired_focus(window)
        record = self.last_snapshot_record
        now = time.monotonic()
        if proof is None:
            self.missing_close_reason = "focus_or_geometry_unproven"
            return False
        if (record is None or self.latest_observation_window is not window or record[0] is not window or
                not 0 <= now - record[1] <= AD_NAV_SNAPSHOT_MAX_SECONDS or self.last_action > record[1]):
            self.missing_close_reason = "snapshot_not_latest_or_expired"
            return False
        if self.observed_navigation_back(window) is None:
            self.missing_close_reason = "native_back_not_observed"
            return False
        token = window.get("native_navigation_observation")
        handler = getattr(self.device, "click_window_action", None)
        if not isinstance(token, str) or not token.startswith("xenoid-native-navigation/v1:") or not callable(handler):
            self.missing_close_reason = "native_navigation_offer_unavailable"
            return False
        result = handler({"observation": token}, "back")
        if not isinstance(result, dict) or result.get("official_backend") != "xenoid_native":
            raise RuntimeError("Official native navigation was not confirmed")
        self.last_navigation_result = {key: result.get(key) for key in
            ("handled", "action", "reason", "official_backend", "input_sent", "verification_seconds", "observation_age_seconds", "verification_stages")}
        if result.get("reason") != self.last_navigation_reported_reason:
            self.log("Native navigation verification " + json.dumps(self.last_navigation_result, separators=(",", ":")))
            self.last_navigation_reported_reason = result.get("reason")
        if result.get("handled") is not True:
            code = result.get("reason", "native_navigation_not_sent")
            self.missing_close_reason = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,96}", code) else "native_navigation_not_sent"
            return False
        if result.get("input_sent") is not True or result.get("action") != "back":
            raise RuntimeError("Official native navigation input was not confirmed")
        self.invalidate_idle_monitor()
        self.last_action = time.monotonic()
        self.ad_progress_wait = None
        self.ad_progress_blocked = False
        self.missing_close_reason = "observed_native_navigation_sent"
        self.log("Input sent: " + reason)
        return True


    def tap(self, control, reason):
        return self.tap_point(*center(bounds(control)), reason)

    def tap_point(self, x, y, reason):
        self.invalidate_idle_monitor()
        if TAP_BACKEND != "native":
            raise ValueError("TAP_BACKEND must be 'native'; this scenario requires official Xenoid native input")
        self.device.tap(x, y)
        self.last_action = time.monotonic()
        self.log("Input sent: " + reason)
        return True


    def check_target_crash(self):
        if self.target_crash:
            raise RuntimeError("The assigned app crashed during this QA attempt: " + self.target_crash)

    def close_crash_dialog(self, window):
        if not is_crash_dialog(window):
            return False
        component = normalized(window.get("component", ""))
        target_pattern = r"application (?:error|not responding):\s*" + re.escape(self.package.casefold()) + r"(?:$|\s)"
        if self.package and re.search(target_pattern, component):
            self.target_crash = window.get("component", self.package)
            self.log("Assigned app crash detected: " + self.target_crash)
        if not DISMISS_ANDROID_CRASH_DIALOGS:
            return False
        if time.monotonic() - self.last_action < ACTION_COOLDOWN_SECONDS:
            return False
        selected = choose_unique([(120, control) for control in controls(window)
                                  if control.get("resource_id", "") in CRASH_CLOSE_IDS])
        if selected is None:
            selected = choose_unique([(100, control) for control in controls(window)
                                      if labels(control) & CRASH_CLOSE_WORDS])
        if selected is None:
            return False
        try:
            result = self.tap(selected, "close Android crash dialog (Bluetooth or app keeps stopping)")
        except (RuntimeError, OSError) as error:
            if str(error) != self.last_crash_error:
                self.log("Waiting to close Android crash dialog: " + str(error))
                self.last_crash_error = str(error)
            return False
        self.last_crash_error = None
        return result

    def service_crash_once(self):
        # A normal dwell does not need an expensive UIAutomator process. Probe
        # just the focused window, and dump controls only for an error overlay.
        try:
            focus = self.device.adb(CRASH_FOCUS_COMMAND)
        except (RuntimeError, OSError) as error:
            if str(error) != self.last_crash_probe_error:
                self.log("Waiting for crash-window probe: " + str(error))
                self.last_crash_probe_error = str(error)
            return False
        self.last_crash_probe_error = None
        text = normalized(focus)
        hinted = any(marker in text for marker in CRASH_FOCUS_HINTS)
        ordinary = bool(re.search(r"mCurrentFocus=Window\{[^}]*\bu\d+\s+[^ /]+/[^}]+\}", str(focus)))
        empty = not text or "mcurrentfocus=null" in text
        fallback = (not hinted and not ordinary and not empty
                    and time.monotonic() - self.last_crash_fallback >= CRASH_FALLBACK_SECONDS)
        if not hinted and not fallback:
            return False
        if fallback:
            self.last_crash_fallback = time.monotonic()
        window = self.observe()
        return bool(window and self.close_crash_dialog(window))

    def close_control(self, window, selectors=(), *, geometric=False, geometry_window=None):
        selected = explicit_control(window, selectors)
        if selected:
            return selected
        scored = []
        ad = is_ad(window) and not is_terms(window)
        for control in controls(window):
            words = labels(control)
            tail = control.get("resource_id", "").rsplit("/", 1)[-1].casefold()
            actionable = control.get("clickable") or control.get("class_name", "").rsplit(".", 1)[-1] in {"Button", "ImageButton"}
            zero_timer = ad and actionable and any(re.fullmatch(
                r"(?:close|skip|dismiss)(?: ad)?\s*(?:in|after)?\s*\(?0\)?\s*(?:s|sec|seconds?)?", word)
                for word in words)
            if zero_timer:
                cleared = dict(control)
                for field in ("text", "content_desc"):
                    if re.fullmatch(r"(?:close|skip|dismiss)(?: ad)?\s*(?:in|after)?\s*\(?0\)?\s*(?:s|sec|seconds?)?",
                                    normalized(control.get(field, ""))):
                        cleared[field] = "Close"
                zero_timer = not close_rejected(cleared, ad=ad)
            if close_rejected(control, ad=ad) and not zero_timer:
                continue
            if zero_timer:
                scored.append((100, control))
                continue
            if (tail in CLOSE_IDS or ad and actionable and tail in AD_PACKED_CLOSE_IDS
                    or ad and close_tokens(control.get("resource_id", "").rsplit("/", 1)[-1]) & {"close", "dismiss", "skip"}):
                scored.append((110, control))
            elif words & CLOSE_WORDS or ad and actionable and semantic_close_label(control):
                scored.append((100, control))
            elif actionable and words & CLOSE_SYMBOLS:
                scored.append((90, control))
        if scored or not geometric or not ad:
            return choose_ad_close(scored) if ad else choose_unique(scored)
        # An ambiguous semantic match stays ambiguous. Geometry uses only actual
        # observed clickable controls, with no CTA/countdown or descriptive copy.
        all_boxes = [bounds(control) for control in controls(geometry_window or window)]
        width = max((box[2] for box in all_boxes), default=0)
        height = max((box[3] for box in all_boxes), default=0)
        for control in controls(window):
            left, top, right, bottom = bounds(control)
            w, h = right - left, bottom - top
            if (control.get("class_name", "").rsplit(".", 1)[-1] in {"Button", "ImageButton", "ImageView", "View"}
                    and control.get("clickable") and not close_rejected(control, ad=True)
                    and labels(control) <= GEOMETRIC_CLOSE_LABELS
                    and top <= height * .18 and (right <= width * .22 or left >= width * .78)
                    and 0 < w <= width * .18 and 0 < h <= height * .18 and .65 <= w / h <= 1.6):
                scored.append((20, control))
        return choose_unique(scored)

    @staticmethod
    def ad_target_key(window, control):
        return (window.get("package"), window.get("component"),
                control.get("resource_id", ""), normalized(control.get("text", "")),
                normalized(control.get("content_desc", "")), bounds(control))

    def ad_attempt_key(self, window, control):
        # A later creative/dialog with the same Close ID gets its own budget.
        # An unchanged hierarchy cannot consume the entire loop in repeated taps.
        visible_state = tuple((item.get("resource_id", ""), item.get("class_name", ""),
                               normalized(item.get("text", "")), normalized(item.get("content_desc", "")),
                               bounds(item), bool(item.get("clickable")))
                              for item in controls(window))
        return self.ad_target_key(window, control), visible_state

    def ad_control_choices(self, window, *, emit_log=True):
        enumerate_controls = getattr(self.device, "clickable_controls", None)
        if enumerate_controls is not None:
            choices = enumerate_controls(window)
        else:
            # Existing saved SDKs remain compatible with the new editable file.
            choices = [{**item, "index": index} for index, item in enumerate(
                (item for item in window.get("controls", ()) if item.get("clickable") is True), 1)]
        # Some SDKs expose a Close label child as nonclickable, with no parent
        # relationship in their flat snapshot. Preserve that explicit semantic
        # candidate instead of guessing an unrelated geometric parent.
        for item in window.get("controls", ()):
            if item.get("clickable") is not True and self.close_control(
                    {**window, "controls": [{**item, "enabled": True}]}, AD_CLOSE_SELECTORS) is not None:
                choices.append({**item, "index": len(choices) + 1, "selection_basis": "semantic_label"})
        material = json.dumps([{key: item.get(key) for key in (
            "index", "text", "content_desc", "resource_id", "class_name", "package", "bounds",
            "enabled", "clickable", "selection_basis")} for item in choices],
            sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(material.encode()).hexdigest()
        if (emit_log and AD_LIST_CLICKABLE_CONTROLS and is_ad(window) and digest != self.last_ad_controls_digest
                and time.monotonic() - self.last_ad_controls_at >= AD_CONTROL_LOG_INTERVAL_SECONDS):
            self.last_ad_controls_digest = digest
            self.last_ad_controls_at = time.monotonic()
            self.log("Observed ad controls: " + str(len(choices)))
            for choice in choices:
                payload = json.dumps(choice, sort_keys=True, ensure_ascii=False)
                if len(payload) <= 1400:
                    self.log("Ad control " + payload)
                else:
                    parts = [payload[index:index + 1400] for index in range(0, len(payload), 1400)]
                    for index, part in enumerate(parts, 1):
                        self.log(f"Ad control {choice['index']} part {index}/{len(parts)}: " + part)
        return choices

    def ad_close_control(self, window, *, emit_log=True):
        choices = self.ad_control_choices(window, emit_log=emit_log)
        # System navigation is listed for inspection, but belongs to the
        # separately verified last-resort Back path, never Close detection.
        candidates = [item for item in choices if item.get("window_type") != "TYPE_SYSTEM"]
        selected = self.close_control({**window, "controls": candidates}, AD_CLOSE_SELECTORS,
                                      geometric=GEOMETRIC_AD_CLOSE and is_ad(window), geometry_window=window)
        if selected is None:
            return None
        # Enumeration indexes belong to the list, not the legacy control value.
        # Preserve the original snapshot node for existing scripts/diagnostics.
        selected = next((item for item in window.get("controls", ())
                         if all(selected.get(key) == value for key, value in item.items())),
                        {key: value for key, value in selected.items() if key not in {"index", "selection_basis"}})
        key = self.ad_target_key(window, selected)
        attempt = self.ad_attempt_key(window, selected)
        if attempt in self.ad_unsafe_targets or self.ad_close_attempts.get(attempt, 0) >= AD_SAME_TARGET_ATTEMPT_LIMIT:
            return None
        return selected

    def ad_progress(self, window):
        """Read visible timer/progress evidence, including nonclickable text."""
        if not is_ad(window):
            return None
        items = window.get("controls", ())
        height = max((box[3] for item in items if (box := bounds(item))), default=0)
        signals = []
        for item in items:
            box = bounds(item)
            labels_here = labels(item)
            progress_bar = item.get("class_name", "").rsplit(".", 1)[-1] == "ProgressBar"
            for label in labels_here:
                number = re.fullmatch(r"\(?\s*(\d+)\s*(?:s|sec|seconds?|ثانیه)?\s*\)?", label)
                timer_words = bool(close_tokens(label) & {"second", "seconds", "sec", "remaining", "left", "ثانیه"})
                action_words = bool(close_tokens(label) & {"close", "skip", "wait", "dismiss", "بستن", "انتظار", "رد"})
                contextual = re.search(r"\d+", label) if timer_words or action_words and (" in " in label or " after " in label) else None
                if (number and box and height and box[1] <= height * AD_COUNTDOWN_TOP_FRACTION
                        or contextual):
                    value = int((number or contextual)[1] if number else contextual[0])
                    if value > 0:
                        signals.append(("countdown", item.get("resource_id", ""), value))
            if progress_bar:
                percent = next((re.search(r"(\d+(?:\.\d+)?)\s*%", label) for label in labels_here
                                if re.search(r"(\d+(?:\.\d+)?)\s*%", label)), None)
                if percent is not None:
                    value = float(percent[1])
                    if value < 100:
                        signals.append(("progress", item.get("resource_id", ""), value))
                else:
                    signals.append(("unknown", item.get("resource_id", ""), item.get("content_desc", "")))
            if item.get("enabled") is False and self.close_control(
                    {**window, "controls": [{**item, "enabled": True}]}, AD_CLOSE_SELECTORS) is not None:
                signals.append(("unknown", "disabled_close", item.get("resource_id", "")))
        return tuple(signals) if signals else None

    def wait_for_ad_progress(self, window):
        # A real enabled Close wins even if a decorative progress view remains.
        if self.close_control(window, AD_CLOSE_SELECTORS, geometric=False) is not None:
            self.ad_progress_wait = None
            return False
        signals = self.ad_progress(window)
        if not signals:
            self.ad_progress_wait = None
            return False
        for value in (AD_PROGRESS_WAIT_SECONDS, AD_PROGRESS_STALL_SECONDS):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Ad progress budgets must be finite nonnegative seconds")
        now = time.monotonic()
        key = (window.get("package"), self.canonical_component(window.get("component", "")))
        state = self.ad_progress_wait
        if state is None or state["key"] != key:
            state = {"key": key, "started": now, "changed": now, "signals": signals, "exhausted": False}
            self.ad_progress_wait = state
            self.log("Waiting for observed ad countdown/progress before last-resort Back")
        elif state["signals"] != signals:
            state.update(signals=signals, changed=now, exhausted=False)
        unknown = all(signal[0] == "unknown" for signal in signals)
        expired = (unknown and AD_PROGRESS_WAIT_SECONDS and now - state["started"] >= AD_PROGRESS_WAIT_SECONDS)
        stalled = AD_PROGRESS_STALL_SECONDS and now - state["changed"] >= AD_PROGRESS_STALL_SECONDS
        if expired or stalled:
            if not state["exhausted"]:
                state["exhausted"] = True
                self.log("Ad progress unresolved after configured " + ("wait budget" if expired else "stall budget")
                         + "; allowing only the verified last-resort Back path")
            return False
        self.ad_progress_blocked = True
        return True

    def close_ad_control(self, window, selected):
        key = self.ad_target_key(window, selected)
        self.tap(selected, "close advertisement/popup")
        self.ad_progress_wait = None
        self.ad_progress_blocked = False
        attempt = self.ad_attempt_key(window, selected)
        self.ad_close_attempts[attempt] = self.ad_close_attempts.get(attempt, 0) + 1
        if is_ad(window):
            self.last_ad_close = {"target": attempt, "at": time.monotonic(),
                                  "component": window.get("component", ""),
                                  "package": window.get("package", "")}
        return True

    def service_missing_ad_close(self, window):
        """Use one observed native Back when a confirmed ad exposes no Close.

        Main screens and permission/terms dialogs never enter this path. Numeric
        progress and accessibility window IDs do not create a new ad identity.
        A Back attempt is not readiness: callers still verify the resulting UI.
        """
        if (window.get("stable") is not True or not is_ad_activity(window)
                or is_terms(window) or is_notification(window) or is_crash_dialog(window)
                or window.get("package") == VPN_PACKAGE):
            self.missing_ad_close = None
            self.missing_close_reason = "not_a_confirmed_ad"
            return False
        if self.wait_for_ad_progress(window):
            self.missing_ad_close = None
            self.missing_close_reason = "ad_countdown_or_progress_wait"
            return False
        # An ambiguous pair of real Close controls is not an absent Close.
        if any(self.close_control({**window, "controls": [control]}, AD_CLOSE_SELECTORS,
                                  geometric=False) is not None for control in controls(window)):
            self.missing_ad_close = None
            self.missing_close_reason = "semantic_close_present_or_ambiguous"
            return False
        def semantic_text(value):
            return re.sub(r"\d+(?:\.\d+)?", "#", normalized(value))
        content = tuple(sorted({(control.get("class_name", ""), control.get("resource_id", ""),
                                 semantic_text(control.get("text", "")), semantic_text(control.get("content_desc", "")),
                                 bool(control.get("clickable"))) for control in controls(window)}))
        proof = self.paired_focus(window)
        identity = ((proof["token"], proof["user"], proof["display_id"]) if proof is not None else None)
        key = (window.get("package"), self.canonical_component(window.get("component", "")), identity, content)
        now = time.monotonic()
        if self.missing_ad_close is None or self.missing_ad_close[0] != key:
            # Only the broker's matched independent WM samples may credit a
            # slow inspection. Time before RPC dispatch/setup cannot count.
            credited = (min(AD_MISSING_CLOSE_BACK_SECONDS, proof["elapsed_seconds"])
                        if proof is not None and proof.get("basis") == "independent_wm_focus_pair" else 0.0)
            if self.ad_progress_blocked:
                credited = 0.0
                self.ad_progress_blocked = False
            self.missing_ad_close = key, now - credited
        self.missing_close_grace_seconds = max(0.0, now - self.missing_ad_close[1])
        if (now - self.missing_ad_close[1] < AD_MISSING_CLOSE_BACK_SECONDS
                or now - self.last_action < ACTION_COOLDOWN_SECONDS
                or self.missing_ad_close_backs.get(key, 0) >= AD_MISSING_CLOSE_BACK_LIMIT):
            self.missing_close_reason = ("grace_not_elapsed" if self.missing_close_grace_seconds < AD_MISSING_CLOSE_BACK_SECONDS
                else "action_cooldown" if now - self.last_action < ACTION_COOLDOWN_SECONDS else "back_attempt_budget_exhausted")
            return False
        reason = ("leave confirmed ad with partial app hierarchy via observed Android Back"
                  if application_hierarchy_missing(window) else "dismiss ad with no exposed Close via observed Android Back")
        if not self.tap_observed_navigation(window, reason):
            return False
        self.missing_ad_close_backs[key] = self.missing_ad_close_backs.get(key, 0) + 1
        self.missing_ad_close = None
        return True

    def return_from_ad_landing(self, window):
        """Use a freshly observed SystemUI Back control after an ad redirect.

        Sending Back is not proof that the ad closed. Both callers still require
        fresh unobstructed app observations afterwards. No key-injection fallback.
        """
        now = time.monotonic()
        origin = self.ad_landing_origin
        if origin is not None:
            same_ad = (window.get("stable") is True and is_ad_activity(window)
                       and window.get("package") == origin["package"]
                       and self.canonical_component(window.get("component", "")) == origin["component"])
            if now - origin["at"] > AD_LANDING_PROVENANCE_SECONDS:
                self.ad_landing_origin = None
            elif same_ad:
                if origin.get("observed_redirect") and self.ad_close_control(window) is not None:
                    return False  # A newly returned ad exposes its real Close control.
                if self.wait_for_ad_progress(window):
                    return False
                # Store Back can reveal the same ad instead of the app. Only
                # that proven origin receives one separately bounded Back.
                # An unchanged ad after this tap still fails normal readiness.
                if (origin["attempts"] >= AD_LANDING_ORIGIN_BACK_LIMIT
                        or now - self.last_action < ACTION_COOLDOWN_SECONDS):
                    return False
                if not self.tap_observed_navigation(window,
                        "dismiss originating ad after store return via observed Android Back"):
                    return False
                origin["attempts"] += 1
                return True
            elif window.get("package") == self.package and not is_ad(window):
                self.ad_landing_origin = None
        previous = self.last_ad_close
        automatic = self.observed_post_connect_ad
        # Some creatives open their configured store without any close tap.
        # Only the real owned AdActivity observed during this cycle's completed
        # post-permission dwell may explain that landing. Its original read time
        # is retained; the finite allowance covers the dwell plus recovery.
        automatic_age_limit = max(START_WAIT_SECONDS) + CONNECTED_WAIT_SECONDS + AD_LANDING_PROVENANCE_SECONDS
        from_dwell = (self.post_connect_dwell_complete and automatic is not None
                      and 0 <= now - automatic["at"] <= automatic_age_limit
                      and (previous is None or previous["at"] < automatic["at"]))
        if from_dwell:
            previous = automatic
        known_origin = (previous is not None and
                        (from_dwell or now - previous["at"] <= AD_LANDING_PROVENANCE_SECONDS))
        configured_return = (self.post_connect_dwell_complete and
                             not AD_LANDING_REQUIRE_OBSERVED_AD and not known_origin)
        if (window.get("package") not in AD_LANDING_PACKAGES
                or not known_origin and not configured_return):
            return False
        if known_origin and not from_dwell:
            self.ad_unsafe_targets.add(previous["target"])
        if self.ad_landing_returns >= AD_LANDING_RETURN_LIMIT:
            raise RuntimeError("Configured ad landing return limit exhausted; assigned app is not ready")
        if now - self.last_action < ACTION_COOLDOWN_SECONDS:
            return False
        if not self.tap_observed_navigation(window, "return from ad landing via observed Android Back"):
            return False
        self.ad_landing_returns += 1
        if configured_return:
            # The panel explicitly names this destination. Do not manufacture
            # an unseen AdActivity or authorize a second same-origin Back.
            self.ad_landing_origin = None
            self.log("Returned from configured ad destination after completed dwell: " + window["package"])
            return True
        self.log(("Observed post-Connect ad reached configured external package: " if from_dwell
                  else "Ad close reached configured external package: ") + window["package"]
                 + "; using observed Android Back without reset")
        self.ad_landing_origin = {"package": previous["package"],
                                  "component": self.canonical_component(previous["component"]),
                                  "at": time.monotonic(), "attempts": 0,
                                  "observed_redirect": from_dwell}
        return True

    def observe_post_connect_ad(self, window):
        if (self.post_connect_dwell_active and window.get("package") == self.package
                and is_ad_activity(window) and self.paired_focus(window) is not None
                and not is_terms(window) and not is_notification(window) and not is_crash_dialog(window)):
            self.observed_post_connect_ad = {"package": self.package,
                "component": self.canonical_component(window.get("component", "")),
                "at": time.monotonic()}

    @staticmethod
    def observed_navigation_back(window):
        if window.get("stable") is not True or type(window.get("display_id")) is not int:
            return None
        matches = []
        for control in window.get("navigation_controls", ()):
            box = bounds(control)
            viewport = control.get("window_bounds")
            if (control.get("resource_id") != "com.android.systemui:id/back"
                    or control.get("package") != "com.android.systemui"
                    or control.get("enabled") is not True or control.get("clickable") is not True
                    or control.get("window_type") != "TYPE_SYSTEM"
                    or control.get("window_title") != "Navigation bar"
                    or type(control.get("window_id")) is not int or type(control.get("window_layer")) is not int
                    or control.get("display_id") != window.get("display_id")
                    or box is None or not isinstance(viewport, (list, tuple)) or len(viewport) != 4
                    or any(type(value) is not int for value in viewport)
                    or not (viewport[0] <= box[0] < box[2] <= viewport[2]
                            and viewport[1] <= box[1] < box[3] <= viewport[3])):
                continue
            matches.append((1, control))
        return choose_unique(matches)

    def ad_failure_evidence(self, window):
        """Record bounded metadata from the held ad snapshot, never another read.

        No screenshot, URLs, free-form ad copy or editable field contents enter
        the event log. Label hashes and recognized policy words distinguish
        anonymous, rejected, ambiguous and exhausted close observations.
        """
        if not is_ad_activity(window) or is_terms(window) or is_notification(window) or is_crash_dialog(window):
            return
        items = list(window.get("controls", ()))
        encoded = json.dumps(window, sort_keys=True, separators=(",", ":"), default=str).encode()
        selected = self.close_control(window, AD_CLOSE_SELECTORS, geometric=False)
        available = self.ad_close_control(window, emit_log=False)
        candidates = sum(self.close_control({**window, "controls": [item]}, AD_CLOSE_SELECTORS,
                                            geometric=False) is not None for item in controls(window))
        reason = ("application_hierarchy_unavailable" if application_hierarchy_missing(window) else
                  "no_semantic_close" if not candidates else "ambiguous_semantic_close" if selected is None
                  else "close_attempt_budget_exhausted" if available is None else "close_still_observed")
        vocabulary = CLOSE_LABEL_TOKENS | CLOSE_REJECT_TOKENS | AD_CLOSE_INFO_TOKENS
        rows, used = [], 0
        # Actionable/control-ID nodes precede anonymous layout containers when
        # the output budget is insufficient for every observed node.
        ordered = sorted(enumerate(items), key=lambda pair: (
            not bool(pair[1].get("clickable")), not bool(pair[1].get("resource_id")), pair[0]))
        for index, item in ordered[:128]:
            rid = str(item.get("resource_id", ""))
            safe_id = rid if (not rid or re.fullmatch(
                r"(?:[A-Za-z_][A-Za-z0-9_.]*:id/)?[A-Za-z_][A-Za-z0-9_.$-]{0,100}", rid)) else "sha256:" + hashlib.sha256(rid.encode()).hexdigest()
            label_values = [str(item.get(field, "")) for field in ("text", "content_desc")]
            tokens = set().union(*(close_tokens(value) for value in label_values))
            cls = str(item.get("class_name", ""))
            row = {"index": index, "id": safe_id, "class": cls if re.fullmatch(r"[A-Za-z0-9_.$]{0,120}", cls) else "unrecognized",
                   "bounds": bounds(item), "enabled": item.get("enabled") is True,
                   "clickable": item.get("clickable") is True,
                   "window": item.get("window_id"), "display": item.get("display_id"),
                   "label_sha256": hashlib.sha256(json.dumps(label_values, ensure_ascii=False).encode()).hexdigest(),
                   "policy_words": sorted(tokens & vocabulary)[:24], "other_word_count": len(tokens - vocabulary),
                   "positive_countdown": any(int(n) > 0 for value in label_values for n in re.findall(r"\d+", value)),
                   "rejected": close_rejected(item, ad=True)}
            line = "Ad control evidence " + json.dumps(row, separators=(",", ":"))
            if len(line.encode()) > 1600 or used + len(line.encode()) + 1 > 24576:
                continue
            rows.append(line)
            used += len(line.encode()) + 1
        proof = self.paired_focus(window)
        back = self.observed_navigation_back(window)
        record = self.last_snapshot_record
        held_age = (max(0.0, time.monotonic() - record[1]) if record is not None and record[0] is window else None)
        snapshot_sha = hashlib.sha256(encoded).hexdigest()
        window_rows, window_used = [], 0
        for index, item in enumerate(window.get("windows", ())[:16]):
            row = {key: item.get(key) for key in
                   ("window_id", "window_layer", "window_type", "window_bounds", "window_active", "window_focused",
                    "display_id", "root_node_present_in_XML", "root_package", "raw_node_count")}
            row.update(snapshot_sha256=snapshot_sha, index=index, control_count=len(item.get("controls", ())))
            line = "Ad window evidence " + json.dumps(row, separators=(",", ":"))
            if len(line.encode()) > 1600 or window_used + len(line.encode()) + 1 > 8192:
                continue
            window_rows.append(line)
            window_used += len(line.encode()) + 1
        observation = "Ad observation evidence " + json.dumps({
            "schema": "xenoid.ad-observation-evidence/v1", "snapshot_sha256": snapshot_sha,
            "held_snapshot_age_seconds": held_age, "last_read": self.last_read_stats,
            "fallback_reason": self.missing_close_reason, "grace_seconds": self.missing_close_grace_seconds,
            "focus_pair": ({key: proof.get(key) for key in
                ("exact_match", "basis", "elapsed_seconds", "hierarchy_read_seconds", "hierarchy_inspection_seconds",
                 "geometry_match", "geometry", "hierarchy_state", "application_windows", "active_application_windows",
                 "node_age_basis")} if proof is not None else None)}, separators=(",", ":"))
        navigation = "Ad navigation evidence " + json.dumps({
            "schema": "xenoid.ad-navigation-evidence/v1", "snapshot_sha256": snapshot_sha,
            "last_native_navigation": self.last_navigation_result,
            "native_back_bounds": back.get("bounds") if back else None,
            "native_back_window_bounds": back.get("window_bounds") if back else None}, separators=(",", ":"))
        observation_ok, navigation_ok = len(observation.encode()) <= 1600, len(navigation.encode()) <= 1600
        self.log("Ad close evidence " + json.dumps({"schema": "xenoid.ad-close-evidence/v1",
                 "snapshot_sha256": snapshot_sha, "reason": reason,
                 "controls_total": len(items), "controls_recorded": len(rows),
                 "controls_omitted": len(items) - len(rows), "semantic_candidates": candidates,
                 "observed_native_back": self.observed_navigation_back(window) is not None,
                 "back_attempts": sum(self.missing_ad_close_backs.values()),
                 "application_hierarchy_missing": application_hierarchy_missing(window),
                 "window_count": len(window.get("windows", ())), "windows_recorded": len(window_rows),
                 "windows_omitted": max(0, len(window.get("windows", ())) - len(window_rows)),
                 "observation_recorded": observation_ok, "navigation_recorded": navigation_ok,
                 "source": "held_snapshot_no_additional_read"}, separators=(",", ":")))
        if observation_ok:
            self.log(observation)
        if navigation_ok:
            self.log(navigation)
        for line in window_rows:
            self.log(line)
        for line in rows:
            self.log(line)

    def ad_failure(self, message):
        window = self.last_ad_window or {}
        owner = str(window.get("package", "unknown"))
        component = str(window.get("component", "unknown"))
        # Diagnose the exact already-observed state that exhausted the budget.
        # Reading again here could hide a transient blocker or add UI work.
        visible = controls(window)
        self.ad_failure_evidence(window)
        diagnostic = {"stable": window.get("stable") is True,
                      "loading": is_loading(window, self.package),
                      "terms": is_terms(window), "notification": is_notification(window),
                      "ad": is_ad(window), "blocking_ad": is_blocking_ad(window, self.package),
                      "control_count": len(window.get("controls", ())),
                      "enabled_control_count": len(visible),
                      "connect_label_matches": sum(control.get("package", self.package) == self.package
                                                    and bool(labels(control) & CONNECT_WORDS)
                                                    for control in visible),
                      "connect_selector_matches": sum(any(matches(control, selector) for selector in CONNECT_SELECTORS)
                                                       for control in visible),
                      "active_windows": [{key: item.get(key) for key in
                                          ("window_id", "window_layer", "window_type", "window_bounds",
                                           "window_active", "window_focused", "display_id")}
                                         for item in window.get("windows", ())
                                         if item.get("window_active") or item.get("window_focused")]}
        return RuntimeError(message + " | foreground=" + owner + " | component=" + component
                            + " | recorded_close_attempts=" + str(sum(self.ad_close_attempts.values()))
                            + " | predicates=" + json.dumps(diagnostic, separators=(",", ":")))

    def terms_close_control(self, window):
        selected = self.close_control(window, TERMS_CLOSE_SELECTORS)
        if selected:
            return selected
        titles = [control for control in window.get("controls", ())
                  if bounds(control) and any(word in value for value in labels(control)
                         for word in ("data collection disclosure", "شفافیت جمع آوری داده"))]
        if len(titles) != 1:
            return None
        title = titles[0]
        title_box = bounds(title)
        screen_width = max((bounds(control)[2] for control in controls(window)), default=0)
        rtl = any("شفافیت" in value for value in labels(title))
        candidates = []
        for control in controls(window):
            if labels(control) or ("Button" not in control.get("class_name", "")
                                   and not control.get("clickable")):
                continue
            left, top, right, bottom = bounds(control)
            width, height = right - left, bottom - top
            leading = left >= screen_width * .7 if rtl else right <= screen_width * .3
            title_height = title_box[3] - title_box[1]
            header = (bottom <= title_box[1]
                      or abs(center(bounds(control))[1] - center(title_box)[1]) <= max(height, title_height))
            if leading and header and 0 < width <= screen_width * .2 and .65 <= width / height <= 1.5:
                candidates.append((1, control))
        return choose_unique(candidates)

    def service_dialog(self, window, *, vpn=False, ads=False):
        # Crash overlays take priority in every phase, including waits and cleanup.
        if is_crash_dialog(window):
            return self.close_crash_dialog(window)
        if time.monotonic() - self.last_action < ACTION_COOLDOWN_SECONDS:
            return False
        if ads and self.return_from_ad_landing(window):
            return True
        package = window.get("package")
        if package == VPN_PACKAGE:
            if not vpn:
                return False
            selected = explicit_control(window, ({"resource_id": "android:id/button1"},))
            if selected:
                return self.tap(selected, "approve Android VPN permission")
            return False
        if is_notification(window):
            selected = choose_unique([(100, control) for control in controls(window)
                                      if control.get("resource_id", "").endswith(":id/permission_allow_button")])
            if selected is None:
                selected = choose_unique([(80, control) for control in controls(window)
                                          if labels(control) & {"allow", "اجازه دادن", "مجاز"}])
            if selected:
                return self.tap(selected, "allow notification permission")
            return False
        if is_ump_consent(window):
            if window.get("package") != self.package:
                return False
            selected = ump_decline_control(window)
            if selected:
                return self.tap(selected, "decline UMP personal-data consent")
            return False
        if is_terms(window):
            selected = self.terms_close_control(window)
            if selected:
                return self.tap(selected, "close terms/disclosure")
            return False
        if ads:
            selected = self.ad_close_control(window)
            if selected:
                return self.close_ad_control(window, selected)
            return self.service_missing_ad_close(window)
        return False


    def wait(self, seconds, *, dialogs=False, vpn=False, ads=False, fail_on_target_crash=True):
        if dialogs:
            values = (IDLE_FOCUS_POLL_SECONDS, IDLE_UI_MIN_SECONDS, IDLE_UI_MAX_SECONDS)
            if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    for value in values) or IDLE_UI_MAX_SECONDS < IDLE_UI_MIN_SECONDS):
                raise ValueError("Use positive finite idle polling intervals; maximum must cover minimum")
            self.invalidate_idle_monitor()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            handled = False
            if dialogs:
                window = self.observe_idle()
                if window:
                    if not ads:
                        self.observe_post_connect_ad(window)
                    handled = self.service_dialog(window, vpn=vpn, ads=ads)
                if handled:
                    self.invalidate_idle_monitor()
            else:
                self.service_crash_once()
            if fail_on_target_crash:
                self.check_target_crash()
            # After input, immediately read again rather than reusing the old
            # controls or waiting for the idle backoff. The action cooldown still
            # applies to that fresh window and prevents a repeated instant tap.
            if handled:
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                if dialogs:
                    until_ui = self._idle_ui_due - time.monotonic()
                    cadence = min(IDLE_FOCUS_POLL_SECONDS,
                                  until_ui if until_ui > 0 else IDLE_UI_MIN_SECONDS)
                else:
                    cadence = CRASH_POLL_SECONDS
                time.sleep(min(cadence, remaining))

    def invalidate_idle_monitor(self):
        self._idle_focus = None
        self._idle_ui_due = float("-inf")
        self._idle_ui_interval = None
        self._idle_action_seen = self.last_action

    def observe_idle(self):
        """Return only a fresh full snapshot, or None when no read is due.

        No hierarchy or coordinates are cached. Focus changes and crash hints
        bypass the backoff; periodic full reads cover same-window UI changes.
        """
        now = time.monotonic()
        try:
            focus = self.device.adb(CRASH_FOCUS_COMMAND)
            self.last_crash_probe_error = None
        except (RuntimeError, OSError) as error:
            if str(error) != self.last_crash_probe_error:
                self.log("Waiting for idle-window probe: " + str(error))
                self.last_crash_probe_error = str(error)
            focus = ""
        signature = normalized(focus)
        ordinary = bool(re.search(r"mCurrentFocus=Window\{[^}]*\bu\d+\s+[^ /]+/[^}]+\}", str(focus)))
        hinted = any(marker in signature for marker in CRASH_FOCUS_HINTS)
        changed = signature != self._idle_focus or self.last_action != self._idle_action_seen
        if not (changed or hinted or not ordinary or now >= self._idle_ui_due):
            return None
        self._idle_focus = signature
        self._idle_action_seen = self.last_action
        window = self.observe()
        finished = time.monotonic()
        if window is None or changed or hinted or not ordinary:
            interval = IDLE_UI_MIN_SECONDS
        else:
            interval = min(IDLE_UI_MAX_SECONDS,
                           (self._idle_ui_interval or IDLE_UI_MIN_SECONDS) * 2)
        self._idle_ui_interval = interval
        self._idle_ui_due = finished + interval
        return window

    def ad_screen_clear(self, window):
        return (window and window.get("stable") is True
                and window.get("package") == self.package
                and not is_loading(window, self.package)
                and not application_hierarchy_missing(window)
                and not is_blocking_ad(window, self.package)
                and not is_crash_dialog(window) and not is_terms(window) and not is_notification(window))

    def confirm_ad_clearance(self, quiet_since):
        """Confirm a late clear app without granting another action/recovery budget."""
        if not AD_FINAL_READY_VERIFY_SECONDS:
            return False
        deadline = time.monotonic() + AD_FINAL_READY_VERIFY_SECONDS
        while time.monotonic() < deadline:
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
            window = self.observe()
            if time.monotonic() > deadline:
                return False
            if window is None:
                continue
            if not self.ad_screen_clear(window):
                return False
            if time.monotonic() - quiet_since >= POST_ACTION_QUIET_SECONDS:
                self.log("Fresh app window confirmed clear after the observation deadline")
                return window
        return False

    def dismiss_ads(self):
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (AD_RECOVERY_VERIFY_SECONDS, AD_FINAL_READY_VERIFY_SECONDS)):
            raise ValueError("Use a nonnegative finite post-Back verification allowance")
        deadline = time.monotonic() + AD_READY_SECONDS if AD_READY_SECONDS else None
        recovery_verification_used = False
        quiet_since = None
        while deadline is None or time.monotonic() < deadline:
            window = self.observe()
            if window:
                self.last_ad_window = window
                if self.return_from_ad_landing(window):
                    quiet_since = None
                    time.sleep(POLL_SECONDS)
                    continue
            if window and is_crash_dialog(window):
                self.close_crash_dialog(window)
                self.check_target_crash()
                quiet_since = None
                time.sleep(POLL_SECONDS)
                continue
            if window and (is_terms(window) or is_notification(window) or window.get("package") == VPN_PACKAGE):
                return  # The caller handles blocking dialogs before the foreground hold.
            loading = window and is_loading(window, self.package)
            selected = self.ad_close_control(window) if window and not loading else None
            if selected and time.monotonic() - self.last_action >= ACTION_COOLDOWN_SECONDS:
                self.close_ad_control(window, selected)
                quiet_since = None
            elif window and self.service_missing_ad_close(window):
                quiet_since = None
                if deadline is not None and not recovery_verification_used:
                    deadline = max(deadline, time.monotonic() + AD_RECOVERY_VERIFY_SECONDS)
                    recovery_verification_used = True
            elif self.ad_screen_clear(window):
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= POST_ACTION_QUIET_SECONDS:
                    return
            else:
                quiet_since = None
            time.sleep(POLL_SECONDS)
        if quiet_since is not None and self.confirm_ad_clearance(quiet_since):
            return
        raise self.ad_failure("The advertisement did not expose a usable close control")


    def is_home_window(self, window):
        if window.get("package") == self.package:
            return False
        if not self.home_checked:
            output = self.device.adb(["shell", "cmd", "package", "resolve-activity", "--brief",
                                      "--user", self.user, "-a", "android.intent.action.MAIN",
                                      "-c", "android.intent.category.HOME"])
            found = re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_.]*/[A-Za-z0-9_.$]+)\s*$", output)
            self.home_checked = True
            if len(found) == 1:
                self.home_component = self.canonical_component(found[0])
            else:
                self.log("Android HOME activity could not be resolved; no app was reopened")
        return bool(self.home_component and
                    self.canonical_component(window.get("component", "")) == self.home_component)

    @staticmethod
    def canonical_component(value):
        owner, separator, activity = value.partition("/")
        if not separator:
            return value
        return owner + "/" + (owner + activity if activity.startswith(".") else activity)

    def prepare_screen(self, seconds, *, ads=True, blocking_ads_only=False):
        deadline = time.monotonic() + seconds if seconds else None
        quiet_since = None
        reopened_from_home = False
        while deadline is None or time.monotonic() < deadline:
            window = self.observe()
            if window:
                self.last_ad_window = window
                if ads and self.return_from_ad_landing(window):
                    quiet_since = None
                    time.sleep(POLL_SECONDS)
                    continue
            # A stale Android permission task can outlive the force-stopped app.
            # Closing it may reveal HOME instead of returning to the target app.
            # Recover only that resolved HOME activity, never an arbitrary app.
            foreign = window and window.get("package") != self.package
            known_overlay = window and (is_crash_dialog(window) or is_terms(window)
                                         or is_notification(window) or is_ad_activity(window)
                                         or window.get("package") == VPN_PACKAGE)
            if foreign and not known_overlay and self.is_home_window(window):
                quiet_since = None
                if not reopened_from_home:
                    reopened_from_home = True
                    self.device.app_open()
                    self.log("Reopened assigned app after Android HOME became foreground: " + self.package)
                time.sleep(POLL_SECONDS)
                continue
            loading = window and is_loading(window, self.package)
            ad_actions = ads and (not blocking_ads_only or not window
                                   or is_blocking_ad(window, self.package)
                                   or self.connect_control(window) is None)
            handled = (window and ((loading and ad_actions and self.service_missing_ad_close(window))
                       or (not loading and self.service_dialog(window, ads=ad_actions))))
            self.check_target_crash()
            blocked = (not window or loading or is_crash_dialog(window) or is_terms(window) or is_notification(window)
                       or application_hierarchy_missing(window)
                       or window.get("package") == VPN_PACKAGE or (ads and is_blocking_ad(window, self.package)))
            if handled or blocked:
                quiet_since = None
            elif window.get("package") == self.package:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= POST_ACTION_QUIET_SECONDS:
                    return window
            else:
                quiet_since = None
            time.sleep(POLL_SECONDS)
        if quiet_since is not None:
            confirmed = self.confirm_ad_clearance(quiet_since)
            if confirmed:
                return confirmed
        raise self.ad_failure("App did not return to an unobstructed foreground window")


    def connect_control(self, window):
        if application_hierarchy_missing(window):
            return None
        selected = explicit_control(window, CONNECT_SELECTORS)
        if selected is None:
            selected = choose_unique([(1, control) for control in controls(window)
                                      if control.get("package", self.package) == self.package
                                      and labels(control) & CONNECT_WORDS])
        return selected

    def connect(self, window=None):
        deadline = time.monotonic() + CONNECT_READY_SECONDS if CONNECT_READY_SECONDS else None
        while True:
            if window is None:
                remaining = deadline - time.monotonic() if deadline is not None else 0
                if deadline is not None and remaining <= 0:
                    break
                window = self.prepare_screen(remaining, blocking_ads_only=True)
                if deadline is not None and time.monotonic() >= deadline:
                    break
            selected = self.connect_control(window)
            if selected:
                self.tap(selected, "Connect button")
                return
            if CONNECT_POSITION is not None and not application_hierarchy_missing(window):
                point = CONNECT_POSITION
                if (not isinstance(point, (tuple, list)) or len(point) != 2
                        or any(type(value) is not int for value in point)):
                    raise ValueError("CONNECT_POSITION must be an explicit pair of integer coordinates")
                self.tap_point(*point, "configured Connect position " + str(point))
                return
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                break
            time.sleep(min(POLL_SECONDS, remaining) if remaining is not None else POLL_SECONDS)
            window = None
        raise self.ad_failure("No unique Connect control was found; set CONNECT_SELECTORS or CONNECT_POSITION")

    def vpn_allowed(self):
        try:
            output = self.device.adb(["shell", "appops", "get", "--user", self.user,
                                      self.package, "ACTIVATE_VPN"])
        except RuntimeError as error:
            detail = str(error)
            if detail != self.last_permission_error:
                self.log("Waiting for Android VPN permission state: " + detail)
                self.last_permission_error = detail
            return False
        self.last_permission_error = None
        return bool(re.search(r"\bACTIVATE_VPN\s*:\s*allow\b", output, re.I))

    def approve_vpn(self, *, ads=True):
        deadline = time.monotonic() + VPN_READY_SECONDS if VPN_READY_SECONDS else None
        last_loading = False
        loading_logged = False
        while deadline is None or time.monotonic() < deadline:
            window = self.observe()
            if window:
                last_loading = is_loading(window, self.package)
                if last_loading and not loading_logged:
                    self.log("Waiting for app server list loading to finish")
                    loading_logged = True
                handled = ((last_loading and ads and self.service_missing_ad_close(window))
                           or (not last_loading and self.service_dialog(window, vpn=True, ads=ads)))
                self.check_target_crash()
                # After any click, obtain a fresh hierarchy before declaring authorization.
                expected_window = (window.get("package") == self.package or not ads and is_ad(window))
                unobstructed = (not last_loading and expected_window and not is_crash_dialog(window)
                                and not application_hierarchy_missing(window)
                                and not is_terms(window) and not is_notification(window)
                                and (not ads or not is_blocking_ad(window, self.package)))
                if not handled and unobstructed and self.vpn_allowed():
                    self.log("Android VPN permission confirmed (including previously authorized apps)")
                    return
            time.sleep(POLL_SECONDS)
        if last_loading:
            raise RuntimeError("App server list loading did not finish before the configured wait ended")
        raise RuntimeError("Android VPN permission was not confirmed before the configured wait ended")

    def cleanup(self):
        self.service_crash_once()
        close_error = None
        try:
            self.device.app_close()
            self.log("App closed")
        except RuntimeError as error:
            close_error = error
            self.log("App close failed; attempting explicit package clear: " + str(error))
        # pm clear also force-stops the pinned package. Do not skip it because a
        # separate force-stop readback timed out.
        result = self.device.clear_data(self.package, user=self.user)
        if str(result).strip() != "Success":
            raise RuntimeError("Android did not confirm clearing app data: " + str(result))
        if close_error:
            self.log("App close recovered by Android pm clear")
        self.log("App data cleared; next cycle is scheduled by the panel")

    def run(self):
        self.device.configure_run(package=self.package)
        self.post_connect_dwell_active = False
        self.post_connect_dwell_complete = False
        self.observed_post_connect_ad = None
        self.ad_progress_wait = None
        self.ad_progress_blocked = False
        self.last_ad_controls_digest = None
        self.last_ad_controls_at = float("-inf")
        failure = None
        try:
            if COLD_START_APP:
                self.device.app_close()
                self.log("Existing app instance closed before launch; app data preserved")
            self.device.app_open()
            self.log("App opened: " + self.package)
            ready = self.prepare_screen(DIALOG_READY_SECONDS, blocking_ads_only=True)
            self.connect(ready)
            self.approve_vpn(ads=False)
            dwell = random.randint(*START_WAIT_SECONDS) + CONNECTED_WAIT_SECONDS
            self.log(f"Keeping app open for {dwell}s after VPN permission; ad dismissal follows")
            self.post_connect_dwell_active = True
            try:
                self.wait(dwell, dialogs=True, vpn=True, ads=False)
            finally:
                self.post_connect_dwell_active = False
            self.post_connect_dwell_complete = True
            self.dismiss_ads()
            self.prepare_screen(DIALOG_READY_SECONDS)
            self.log(f"Ad handling completed; keeping app in foreground for {AFTER_AD_WAIT_SECONDS}s")
            self.wait(AFTER_AD_WAIT_SECONDS, dialogs=True, vpn=True, ads=True)
        except Exception as error:
            failure = error
            self.log("Cycle failed: " + type(error).__name__ + ": " + str(error))
        finally:
            try:
                self.cleanup()
            except Exception as error:
                failure = failure or error
                self.log("Cleanup failed: " + type(error).__name__ + ": " + str(error))
        if failure:
            raise failure
        self.check_target_crash()
        self.regenerate_after_success()
        self.log("QA cycle completed successfully")

    def regenerate_after_success(self):
        if not REGENERATE_ENABLED:
            return False
        validate_regeneration_settings()
        low, high = REGENERATE_RANDOM_RANGE
        selected = random.randint(low, high)
        self.log(f"Regeneration draw: {selected} (range {low}..{high}; trigger {REGENERATE_TRIGGER})")
        if selected != REGENERATE_TRIGGER:
            return False
        # Keep this key across transport/recovery attempts. The host persists a
        # receipt so a lost response cannot request a second identity rotation.
        operation_key = uuid.uuid4().hex
        self.log("Official Xenoid regenerate selected; waiting for Android reload")
        cache_packages = list(dict.fromkeys(GOOGLE_CACHE_PACKAGES))
        post_cache = ({"packages": cache_packages, "user": GOOGLE_CACHE_USER,
                       "timeout": GOOGLE_CACHE_TIMEOUT} if cache_packages else None)
        post_google = ({"clear_packages": list(dict.fromkeys(GOOGLE_DATA_PACKAGES)),
                        "user": GOOGLE_CACHE_USER, "delay_seconds": GOOGLE_RESET_DELAY_SECONDS,
                        "timeout": GOOGLE_RESET_TIMEOUT} if GOOGLE_DATA_RESET_ENABLED else None)
        while True:
            try:
                self.invalidate_idle_monitor()
                options = {"post_cache": post_cache}
                if post_google is not None:
                    options["post_google"] = post_google
                result = self.device.regenerate(timeout=REGENERATE_TIMEOUT, operation_key=operation_key, **options)
                required = ("ok", "native_ready", "profile_ready", "container_id_preserved",
                            "instance_id_preserved", "resource_tag_preserved", "identities_changed",
                            "input_ready", "foreground_ready")
                if (not isinstance(result, dict) or result.get("phase") != "committed"
                        or result.get("runtimeOnly") is not True or result.get("containerRecreated") is not False
                        or any(result.get(key) is not True for key in required)):
                    raise RuntimeError("Official regeneration readiness was not confirmed")
                if post_cache is not None:
                    cleanup = result.get("cache_cleanup")
                    if (not isinstance(cleanup, dict) or cleanup.get("ok") is not True
                            or cleanup.get("cache_only") is not True
                            or cleanup.get("data_cleared") is not False
                            or cleanup.get("packages") != cache_packages
                            or cleanup.get("user") != GOOGLE_CACHE_USER):
                        raise RuntimeError("regeneration_post_cache_pending: cache-only proof was not confirmed")
                    for package in cache_packages:
                        self.log("Google cache cleared (cache only): " + package)
                if post_google is not None:
                    reset = result.get("google_reset")
                    if (not isinstance(reset, dict) or reset.get("ok") is not True
                            or reset.get("data_cleared") is not True
                            or reset.get("packages") != post_google["clear_packages"]
                            or reset.get("registration_enabled") is not True
                            or reset.get("checkin_requested") is not True
                            or ("com.google.android.gms" in post_google["clear_packages"]
                                and (reset.get("identity_mode") != "provider-managed"
                                     or reset.get("offline_marker_retired") is not True))):
                        raise RuntimeError("regeneration_post_google_pending: data reset and microG registration were not confirmed")
                    self.log("Google app data cleared after cache; microG device registration enabled and check-in requested")
                break
            except (RuntimeError, OSError) as error:
                self.log("Regeneration waiting: " + str(error))
                # No UI/ADB crash probing while an identity transaction may be
                # pending. Resume the same recorded operation after this pause.
                retry_seconds = (GOOGLE_CACHE_RETRY_SECONDS
                                 if "regeneration_post_cache_pending" in str(error)
                                 else REGENERATE_RETRY_SECONDS)
                time.sleep(retry_seconds)
        # The official restart replays the regenerated native display profile.
        # Restore the administrator's script settings after that replay finishes.
        # A display retry must not invoke regenerate again or draw a new number.
        while True:
            try:
                configure_resources(self.device)
                break
            except (RuntimeError, OSError) as error:
                self.log("Waiting to restore display after regeneration: " + str(error))
                time.sleep(REGENERATE_RETRY_SECONDS)
        self.log("Official Xenoid regeneration committed; Android reload and display settings verified")
        self.invalidate_idle_monitor()
        return True


def validate_regeneration_settings():
    if type(REGENERATE_ENABLED) is not bool:
        raise ValueError("REGENERATE_ENABLED must be Boolean")
    if not REGENERATE_ENABLED:
        return
    if type(GOOGLE_DATA_RESET_ENABLED) is not bool:
        raise ValueError("GOOGLE_DATA_RESET_ENABLED must be Boolean")
    if GOOGLE_DATA_RESET_ENABLED:
        if (not isinstance(GOOGLE_DATA_PACKAGES, (tuple, list)) or not GOOGLE_DATA_PACKAGES
                or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", p)
                       for p in GOOGLE_DATA_PACKAGES)):
            raise ValueError("GOOGLE_DATA_PACKAGES must contain Android package names")
        if (type(GOOGLE_RESET_DELAY_SECONDS) not in (int, float)
                or not math.isfinite(GOOGLE_RESET_DELAY_SECONDS) or GOOGLE_RESET_DELAY_SECONDS < 0
                or type(GOOGLE_RESET_TIMEOUT) is not int or GOOGLE_RESET_TIMEOUT < 0):
            raise ValueError("Invalid Google reset timing")
    if (not isinstance(REGENERATE_RANDOM_RANGE, (tuple, list)) or len(REGENERATE_RANDOM_RANGE) != 2
            or any(type(value) is not int for value in REGENERATE_RANDOM_RANGE)
            or REGENERATE_RANDOM_RANGE[0] > REGENERATE_RANDOM_RANGE[1]
            or type(REGENERATE_TRIGGER) is not int
            or not REGENERATE_RANDOM_RANGE[0] <= REGENERATE_TRIGGER <= REGENERATE_RANDOM_RANGE[1]):
        raise ValueError("Use an integer regeneration range and a trigger inside that range")
    if type(REGENERATE_TIMEOUT) is not int or REGENERATE_TIMEOUT < 0:
        raise ValueError("REGENERATE_TIMEOUT must be a nonnegative integer; 0 is unlimited")
    if (type(REGENERATE_RETRY_SECONDS) not in (int, float)
            or not math.isfinite(REGENERATE_RETRY_SECONDS) or REGENERATE_RETRY_SECONDS < 0):
        raise ValueError("REGENERATE_RETRY_SECONDS must be a nonnegative finite number")
    if (not isinstance(GOOGLE_CACHE_PACKAGES, (tuple, list))
            or any(not isinstance(package, str) or re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", package) is None
                   for package in GOOGLE_CACHE_PACKAGES)):
        raise ValueError("GOOGLE_CACHE_PACKAGES must contain valid Android package names")
    if not (GOOGLE_CACHE_USER == "current" or type(GOOGLE_CACHE_USER) is int and GOOGLE_CACHE_USER >= 0):
        raise ValueError("GOOGLE_CACHE_USER must be current or a nonnegative Android user")
    if type(GOOGLE_CACHE_TIMEOUT) is not int or GOOGLE_CACHE_TIMEOUT < 0:
        raise ValueError("GOOGLE_CACHE_TIMEOUT must be a nonnegative integer; 0 is unlimited")
    if (type(GOOGLE_CACHE_RETRY_SECONDS) not in (int, float)
            or not math.isfinite(GOOGLE_CACHE_RETRY_SECONDS) or GOOGLE_CACHE_RETRY_SECONDS < 0):
        raise ValueError("GOOGLE_CACHE_RETRY_SECONDS must be a nonnegative finite number")


def display_state(device):
    size = device.adb(["shell", "wm", "size"])
    density = device.adb(["shell", "wm", "density"])
    physical_size = re.findall(r"(?m)^Physical size:\s*(\d+)x(\d+)\s*$", size)
    override_size = re.findall(r"(?m)^Override size:\s*(\d+)x(\d+)\s*$", size)
    physical_density = re.findall(r"(?m)^Physical density:\s*(\d+)\s*$", density)
    override_density = re.findall(r"(?m)^Override density:\s*(\d+)\s*$", density)
    if len(physical_size) != 1 or len(physical_density) != 1 or len(override_size) > 1 or len(override_density) > 1:
        raise RuntimeError("Android did not report one physical display size and density")
    native_size = tuple(map(int, physical_size[0]))
    native_density = int(physical_density[0])
    custom_size = tuple(map(int, override_size[0])) if override_size else None
    custom_density = int(override_density[0]) if override_density else None
    if min(*native_size, native_density) <= 0:
        raise RuntimeError("Android returned an invalid physical display")
    return {"physical_size": native_size, "physical_density": native_density,
            "override_size": custom_size, "override_density": custom_density,
            "size": custom_size or native_size, "density": custom_density or native_density}


def configure_resources(device):
    if DISPLAY_FPS is None and DISPLAY_SCALE is None:
        result = device.apply_resource_defaults()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Central device resource settings were not confirmed")
        device.log("Display settings inherited from the panel and applied device profile")
        return result
    # An explicit legacy override changes only the selected settings, avoiding
    # a temporary central FPS apply followed by another graphics restart.
    if DISPLAY_FPS is not None:
        device.set_frame_rate(DISPLAY_FPS, timeout=DISPLAY_SETUP_TIMEOUT)
        device.log("Display frame rate configured: " + ("native" if DISPLAY_FPS == 0 else str(DISPLAY_FPS) + " FPS"))
    return configure_display(device, DISPLAY_SCALE)


def configure_display(device, scale):
    """Apply a source-configured display scale once; retain the true physical base."""
    if scale is None:
        return {"preserved": True, "changed": False}
    minimum = scale == "minimum"
    if minimum:
        if (type(MIN_DISPLAY_SIDE) is not int or MIN_DISPLAY_SIDE <= 0
                or type(MIN_DISPLAY_DPI) is not int or MIN_DISPLAY_DPI <= 0):
            raise ValueError("MIN_DISPLAY_SIDE and MIN_DISPLAY_DPI must be positive integers")
    else:
        if type(scale) not in (int, float):
            raise ValueError("DISPLAY_SCALE must be 'minimum', None or a positive finite number")
        try:
            valid_scale = math.isfinite(scale) and scale > 0
        except OverflowError:
            valid_scale = False
        if not valid_scale:
            raise ValueError("DISPLAY_SCALE must be 'minimum', None or a positive finite number")
    before = display_state(device)
    # Read and retain both physical values before any write. wm keeps reporting
    # these originals even when a previous cycle has already installed overrides.
    physical_size = before["physical_size"]
    physical_density = before["physical_density"]
    factor = max(MIN_DISPLAY_SIDE / value for value in physical_size) if minimum else scale
    products = [value * factor for value in (*physical_size, physical_density)]
    if not all(math.isfinite(value) for value in products):
        raise ValueError("DISPLAY_SCALE produces invalid display dimensions")
    minimum_even = max(2, ((MIN_DISPLAY_SIDE + 1) // 2) * 2) if minimum else 2
    desired_size = tuple(max(minimum_even, int(round(value / 2)) * 2) for value in products[:2])
    desired_density = MIN_DISPLAY_DPI if minimum else max(1, int(round(products[2])))
    native = not minimum and scale == 1
    if native:
        # Reset keeps the exact physical dimensions (which need not be even).
        desired_size, desired_density = physical_size, physical_density
    commands = []
    if native:
        if before["override_size"] is not None:
            commands.append((["shell", "wm", "size", "reset"], "size"))
        if before["override_density"] is not None:
            commands.append((["shell", "wm", "density", "reset"], "density"))
    else:
        if before["size"] != desired_size:
            commands.append((["shell", "wm", "size", f"{desired_size[0]}x{desired_size[1]}"], "size"))
        if before["density"] != desired_density:
            commands.append((["shell", "wm", "density", str(desired_density)], "density"))
    attempted = []
    try:
        for command, kind in commands:
            attempted.append(kind)
            device.adb(command)
        after = display_state(device) if commands else before
        if after["size"] != desired_size or after["density"] != desired_density:
            raise RuntimeError("Android display override did not match the requested scale")
    except Exception:
        # A failed second wm command must not leave a half-applied configuration.
        for kind in reversed(attempted):
            original = before["override_size"] if kind == "size" else before["override_density"]
            value = (f"{original[0]}x{original[1]}" if kind == "size" and original is not None
                     else str(original) if original is not None else "reset")
            try:
                device.adb(["shell", "wm", kind, value])
            except Exception as rollback_error:
                device.log("Display rollback needs attention: " + str(rollback_error))
        raise
    summary = {"preserved": False, "changed": bool(commands), "scale": scale,
               "physical_size": physical_size, "physical_density": physical_density,
               "effective_size": desired_size, "effective_density": desired_density}
    device.log("Display scale " + str(scale) + ": "
               + f"{physical_size[0]}x{physical_size[1]}@{physical_density} -> "
               + f"{desired_size[0]}x{desired_size[1]}@{desired_density}"
               + (" (updated)" if commands else " (already applied)"))
    return summary


def main():
    device = Device()
    cycle = None
    try:
        cycle = Cycle(device, "")
        cycle.service_crash_once()
        # The assignment is the only source of the target; never use foreground fallback.
        device.configure_run(package=None, verify_apk=True)
        package = device.app_package()
        if not isinstance(package, str) or not package:
            raise ValueError("The device has no resolvable assigned APK")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", package):
            raise ValueError("Invalid target app package")
        if (len(START_WAIT_SECONDS) != 2 or any(type(value) is not int for value in START_WAIT_SECONDS)
                or START_WAIT_SECONDS[0] < 0
                or START_WAIT_SECONDS[1] < START_WAIT_SECONDS[0] or POLL_SECONDS <= 0
                or CRASH_POLL_SECONDS <= 0 or FAILED_CYCLE_BACKOFF_SECONDS < 0
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                       for value in (AFTER_AD_WAIT_SECONDS, CONNECTED_WAIT_SECONDS,
                                     AD_PROGRESS_WAIT_SECONDS, AD_PROGRESS_STALL_SECONDS, AD_FINAL_READY_VERIFY_SECONDS,
                                     AD_CONTROL_LOG_INTERVAL_SECONDS))
                or type(AD_LANDING_REQUIRE_OBSERVED_AD) is not bool
                or type(AD_COUNTDOWN_TOP_FRACTION) not in (int, float)
                or not 0 < AD_COUNTDOWN_TOP_FRACTION <= 1):
            raise ValueError("Invalid script timing settings")
        validate_regeneration_settings()
        cycle.package = package
        configure_resources(device)
        cycle.run()
    except (RuntimeError, OSError) as error:
        device.log("QA attempt failed: " + type(error).__name__ + ": " + str(error))
        if not CONTINUE_AFTER_CYCLE_ERROR:
            raise
        device.log(f"Retry requested after {FAILED_CYCLE_BACKOFF_SECONDS}s; this attempt is not successful")
        if cycle is not None:
            cycle.wait(FAILED_CYCLE_BACKOFF_SECONDS, fail_on_target_crash=False)
        else:
            time.sleep(FAILED_CYCLE_BACKOFF_SECONDS)
        raise SystemExit(RETRY_EXIT_CODE)


if __name__ == "__main__":
    main()
