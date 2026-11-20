"""Check the installer integrity pins against the source-only release bundle."""
import hashlib
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
source = (root / "install.sh").read_text()
pins = {"fleet_sha256": "xenoid-fleet", "toolchain_sha256": "install-toolchain.sh",
        "daemon_patch_sha256": "patches/standalone-manifest.patch",
        "keymint_patch_sha256": "patches/keymint-paths.patch",
        "protection_patch_sha256": "patches/shared-protection-inspect.patch",
        "proxy_acceptance_patch_sha256": "patches/proxy-live-acceptance.patch",
        "proxy_upgrader_sha256": "scripts/upgrade-proxy-control.py",
        "qa_manifest_sha256": "qa/manifest.sha256", "qa_installer_sha256": "install-qa-addon.sh"}
for name, filename in pins.items():
    expected = re.search(r"^" + name + r"=([0-9a-f]{64})$", source, re.M)
    assert expected, name + " is not pinned"
    actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
    assert actual == expected[1], filename + " does not match its installer pin"
version = re.search(r'^VERSION = "([^\"]+)"$', (root / "xenoid-fleet").read_text(), re.M)[1]
assert "release=" + version + "\n" in source
print("Release integrity pins match " + version)
