"""Delegate panel creation to the installed Fleet release's native path."""

import json
import os
import runpy
import stat
import sys
from pathlib import Path
from types import SimpleNamespace


FLEET_ROOT = Path("/opt/xenoid-fleet")
GOOGLE_REGISTRATION_ENABLED = "Google Device registration enabled"
# Read the pinned microG preference after making changes through ProvisionService.
GOOGLE_REGISTRATION_COMMAND = r"""
app_data=/data/user/0/com.google.android.gms
preferences=$app_data/shared_prefs/com.google.android.gms_preferences.xml
registration_state() {
    [ -d "$app_data" ] && [ ! -L "$app_data" ] && [ ! -L "$app_data/shared_prefs" ] \
        && [ ! -L "$preferences" ] || return 1
    if [ ! -e "$preferences" ]; then
        echo disabled
        return
    fi
    [ -f "$preferences" ] || return 1
    owner=$(stat -c %u "$app_data") && preference_owner=$(stat -c %u "$preferences") || return 1
    [ "$owner" = "$preference_owner" ] || return 1
    awk '
        /name="checkin_enable_service"/ {
            count++
            if ($0 ~ /^[[:space:]]*<boolean name="checkin_enable_service" value="(true|false)"[[:space:]]*\/>[[:space:]]*$/)
                enabled = ($0 ~ /value="true"/)
            else
                malformed = 1
        }
        END {
            if (malformed || count > 1) exit 1
            print enabled ? "enabled" : "disabled"
        }' "$preferences"
}
state=$(registration_state) || exit 1
if [ "$state" = enabled ]; then
    echo 'Google Device registration enabled'
    exit 0
fi
cmd activity start-service --user 0 -n com.google.android.gms/org.microg.gms.provision.ProvisionService \
    --ez checkin_enabled true >/dev/null || exit 1
for attempt in 1 2 3 4 5; do
    if state=$(registration_state) && [ "$state" = enabled ]; then
        echo 'Google Device registration enabled'
        exit 0
    fi
    sleep 1
done
echo 'microG device registration switch did not turn on' >&2
exit 1
""".strip()


def load_baseline(fleet, digest):
    directory = FLEET_ROOT / "panel-chrome-baseline"
    info = directory.lstat()
    error = fleet["FleetError"]
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_uid, info.st_gid) != (0, 0)):
        raise error("The panel Chrome baseline directory must be private and root-owned")
    private_file = fleet["private_baseline_file"]
    with private_file(directory / ".archive-sha256") as stream:
        if stream.read(66) != digest.encode("ascii") + b"\n":
            raise error("The staged Chrome archive differs from the approved panel archive")
    with private_file(directory / "manifest.json") as stream:
        manifest = json.loads(stream.read(16385))
    if (not isinstance(manifest, dict) or set(manifest) != {"schema", "version_code", "chrome", "trichrome"}
            or manifest["schema"] != fleet["CHROME_SCHEMA"] or type(manifest["version_code"]) is not int
            or not 1 <= manifest["version_code"] <= 2**63 - 1
            or not isinstance(manifest["chrome"], list) or not manifest["chrome"]):
        raise error("The panel requires an APK-only Chrome baseline")
    chrome = tuple(fleet["baseline_apk"](directory, apk) for apk in manifest["chrome"])
    lib = fleet["baseline_apk"](directory, manifest["trichrome"])
    if chrome[0].name != "base.apk" or lib.name != "trichrome.apk" or len({*chrome, lib}) != len(chrome) + 1:
        raise error("The panel Chrome split or Trichrome file names are invalid")
    return fleet["ChromeBaseline"](manifest["version_code"], chrome, lib)


def document(fleet, upstream, device, arguments):
    result, code = upstream.document(arguments, device)
    if code or result.get("ok") is not True:
        raise fleet["FleetError"](f"Could not verify Xenoid {' '.join(arguments[:2])}")
    return result


def enable_google_device_registration(fleet, upstream, device):
    status = document(fleet, upstream, device, ["google-services", "status"])
    if status.get("provider") != "microg":
        return
    mode = status.get("googleIdentityMode")
    if mode == "offline-seeded":
        print(f"phone-{device}: Google Device registration skipped for the offline Google identity.", flush=True)
        return
    if mode != "provider-managed":
        raise fleet["FleetError"]("Could not verify the microG Google identity mode")
    # ProvisionService updates the same setting as the microG UI and requests check-in.
    result, code = upstream.document(["root", "exec", GOOGLE_REGISTRATION_COMMAND], device)
    output = result.get("stdout")
    if code or result.get("ok") is not True or not isinstance(output, str) or output.strip() != GOOGLE_REGISTRATION_ENABLED:
        raise fleet["FleetError"]("Could not enable and verify microG Google Device registration")
    print(f"phone-{device}: microG Google Device registration is enabled.", flush=True)


def create(fleet, upstream, device, profile, baseline, proxy_default):
    fleet["create_device"](upstream, device, SimpleNamespace(no_google_services=False),
                           profile, baseline, proxy_default, reconcile=False)
    enable_google_device_registration(fleet, upstream, device)


def main(version, digest, selector, profile=None):
    fleet = runpy.run_path(str(FLEET_ROOT / "xenoid-fleet"))
    if fleet["VERSION"] != version or (digest != "-" and not fleet["ONE_SHA256"].fullmatch(digest)):
        raise RuntimeError("Unexpected Fleet release or panel archive digest")
    os.umask(0o022)
    upstream = fleet["Upstream"]()
    baseline = None if digest == "-" else load_baseline(fleet, digest)
    proxy_default = fleet["ProxyDefault"].load()
    ids = fleet["device_ids"](selector)
    failed = False
    with upstream.validated_profile(profile) as validated:
        for device in ids:
            try:
                with fleet["device_lock"](device):
                    create(fleet, upstream, device, validated, baseline, proxy_default)
                # Finalize after native profile, proxy, microG and Chrome setup.
                # The wrapper's per-device lock must be released before its QA hook.
                fleet["qa_reconcile"]([device], force=True, required=True)
                fleet["graphics_reconcile"]([device])
            except (OSError, fleet["FleetError"]) as error:
                fleet["report_failure"](device, error)
                failed = True
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main(*sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"panel-device-create: {error}", file=sys.stderr)
        raise SystemExit(1)
