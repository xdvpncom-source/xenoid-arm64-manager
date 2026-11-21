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
        "proxy_routing_patch_sha256": "patches/proxy-domain-routing.patch",
        "proxy_cleanup_patch_sha256": "patches/proxy-namespaced-cleanup.patch",
        "host_vm_patch_sha256": "patches/host-vm-isolation.patch",
        "proxy_upgrader_sha256": "scripts/upgrade-proxy-control.py",
        "qa_manifest_sha256": "qa/manifest.sha256", "qa_installer_sha256": "install-qa-addon.sh"}
for name, filename in pins.items():
    expected = re.search(r"^" + name + r"=([0-9a-f]{64})$", source, re.M)
    assert expected, name + " is not pinned"
    actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
    assert actual == expected[1], filename + " does not match its installer pin"
version = re.search(r'^VERSION = "([^\"]+)"$', (root / "xenoid-fleet").read_text(), re.M)[1]
assert "release=" + version + "\n" in source
installer = (root / "install-qa-addon.sh").read_text()
installed = re.search(r"^for qa_file in (.+); do$", installer, re.M)[1].split()
manifest_rows = [line.split(maxsplit=1) for line in (root / "qa/manifest.sha256").read_text().splitlines()]
manifest_paths = [row[1].strip() for row in manifest_rows]
assert len(manifest_paths) == len(set(manifest_paths)), "Duplicate QA manifest paths"
required = {"script_host.py", "script_runtime.py", "shared_policy.py", "qa_lifecycle.py",
            "qa_vpn_probe.py", "qa_input_ready.py", "qa_ui_reader.py"}
assert required <= set(installed), "Required QA host module is not installed"
assert all(installed.index(name) < installed.index("script_host.py") for name in required - {"script_host.py"}), "Host dependency installs after entrypoint"
for filename in installed:
    assert "qa/" + filename in manifest_paths, filename + " has no integrity pin"
    assert (root / "qa" / filename).is_file(), filename + " is missing from release"
print("Release integrity pins match " + version)
