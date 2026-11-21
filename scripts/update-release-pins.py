#!/usr/bin/env python3
"""Regenerate source bundle checksums before tagging a Fleet release."""
import hashlib
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
required_host_modules = {
    "script_host.py", "script_runtime.py", "shared_policy.py",
    "qa_lifecycle.py", "qa_vpn_probe.py", "qa_input_ready.py", "qa_ui_reader.py", "qa_google_reset.py",
}
missing = sorted(name for name in required_host_modules if not (root / "qa" / name).is_file())
if missing:
    raise SystemExit("Missing required QA host modules: " + ", ".join(missing))
files = sorted([*(root / "qa").glob("*.py"), root / "qa/qa-resources.json",
                *(root / "deploy").glob("*"),
                *(root / "native/xenoid-hwcomposer").glob("*.c"),
                root / "native/xenoid-hwcomposer/build.sh", root / "native/xenoid-hwcomposer/README.txt",
                root / "native/xenoid-gralloc/android_compat.h",
                *(root / "native/xenoid-ui-reader").glob("*")])
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
(root / "qa/manifest.sha256").write_text("".join(
    digest(path) + "  " + str(path.relative_to(root)) + "\n" for path in files))
source = (root / "install.sh").read_text()
for name, filename in {"fleet_sha256": "xenoid-fleet",
                       "proxy_acceptance_patch_sha256": "patches/proxy-live-acceptance.patch",
                       "proxy_routing_patch_sha256": "patches/proxy-domain-routing.patch",
                       "proxy_cleanup_patch_sha256": "patches/proxy-namespaced-cleanup.patch",
                       "host_vm_patch_sha256": "patches/host-vm-isolation.patch",
                       "proxy_upgrader_sha256": "scripts/upgrade-proxy-control.py",
                       "qa_manifest_sha256": "qa/manifest.sha256",
                       "qa_installer_sha256": "install-qa-addon.sh"}.items():
    source, count = re.subn(r"^" + name + r"=\S+$", name + "=" + digest(root / filename), source, flags=re.M)
    assert count == 1, name
(root / "install.sh").write_text(source)
