#!/usr/bin/env python3
"""Offline product-release and fresh-data isolation contracts for Bluejay."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xenoid import google_services as gs
from xenoid import config as config_module
from xenoid import cli as cli_module
from xenoid.cold_profile import load_profile

RAVEN_METADATA_SHA = "f21706e7152c480239b569b353a87a8ff2869d0e368e9e317d0c4eb8b41dd656"
RAVEN_SPEC_SHA = "f569d2ca6dbe1c49a7443ab26c49242a793072dff48143cce9b46a8f381defb1"


class BluejayReleaseTests(unittest.TestCase):
    def setUp(self):
        self.raven = gs.load_release_spec(ROOT, gs.MICROG_PLAY_RELEASE)
        self.bluejay = gs.load_release_spec(ROOT, gs.MICROG_BLUEJAY_RELEASE)
        self.profile = load_profile(ROOT / "examples/fingerprints/pixel-bluejay-android13.json")

    def cfg(self, release, profile=None):
        return SimpleNamespace(google_services_provider=gs.PROVIDER_MICROG,
                               google_services_release=release, auto_build_runtime_image=True,
                               extra_docker_args=[], cold_profile=profile)

    def test_raven_metadata_and_data_identity_are_byte_for_byte_unchanged(self):
        self.assertEqual(hashlib.sha256(self.raven.metadata_path.read_bytes()).hexdigest(), RAVEN_METADATA_SHA)
        self.assertEqual(self.raven.fingerprint, RAVEN_SPEC_SHA)
        self.assertEqual(self.raven.data_compatibility_fingerprint, RAVEN_SPEC_SHA)
        self.assertEqual(self.raven.android["targetProduct"], "raven")
        self.assertEqual(gs.load_release_spec(ROOT).release, gs.MICROG_PLAY_RELEASE)

    def test_only_release_and_target_product_differ_from_raven_metadata(self):
        expected = deepcopy(dict(self.raven.metadata))
        expected["release"] = gs.MICROG_BLUEJAY_RELEASE
        expected["android"]["targetProduct"] = "bluejay"
        self.assertEqual(dict(self.bluejay.metadata), expected)
        self.assertEqual(self.bluejay.components, self.raven.components)
        self.assertEqual(self.bluejay.signature_policy, self.raven.signature_policy)
        self.assertEqual(self.bluejay.product_policy, self.raven.product_policy)
        self.assertNotEqual(self.bluejay.fingerprint, self.raven.fingerprint)
        self.assertNotEqual(self.bluejay.data_compatibility_fingerprint, self.raven.data_compatibility_fingerprint)
        self.assertNotEqual(gs.asset_paths(ROOT, self.bluejay)["manifest"], gs.asset_paths(ROOT, self.raven)["manifest"])

    def test_product_binding_cannot_be_forged_inside_release_metadata(self):
        for original, replacement in ((self.bluejay, "raven"), (self.raven, "bluejay")):
            value = deepcopy(dict(original.metadata))
            value["android"]["targetProduct"] = replacement
            with self.assertRaises(gs.GoogleServicesError):
                gs._validate_release_metadata(value, original.release)
        for field in ("sha256", "signingCertificateHistorySha256"):
            value = deepcopy(dict(self.bluejay.metadata))
            value["components"][0][field] = "a" * 64 if field == "sha256" else ["a" * 64]
            with self.assertRaises(gs.GoogleServicesError):
                gs._validate_release_metadata(value, self.bluejay.release)

    def test_explicit_matching_cold_profile_required_before_asset_access(self):
        context = SimpleNamespace(project_root=ROOT)
        with patch.object(gs, "quick_validate_assets") as assets:
            for cfg in (self.cfg(gs.MICROG_BLUEJAY_RELEASE), self.cfg(gs.MICROG_PLAY_RELEASE, self.profile)):
                with self.assertRaises(gs.GoogleServicesError):
                    gs.resolve_google_runtime_spec(context, cfg, "fresh-create")
            assets.assert_not_called()
            answer = gs.resolve_google_runtime_spec(context, self.cfg(gs.MICROG_BLUEJAY_RELEASE, self.profile), "fresh-create")
            self.assertEqual(answer.fingerprint, self.bluejay.fingerprint)
            assets.assert_called_once_with(ROOT, answer)
        self.assertEqual(gs.resolve_google_runtime_spec(context, self.cfg(gs.MICROG_PLAY_RELEASE),
                                                        "status", require_assets=False).fingerprint, RAVEN_SPEC_SHA)

    def test_legacy_live_config_without_new_attribute_retains_raven_behavior(self):
        new = config_module.new_instance_config("legacy", "11111111-2222-4333-8444-555555555555")
        fields = vars(new).copy()
        fields.pop("cold_profile", None)
        legacy = SimpleNamespace(**fields)
        config_module._validate_config(legacy)
        selected = gs.resolve_google_runtime_spec(SimpleNamespace(project_root=ROOT), legacy,
                                                   "regenerate", require_assets=False)
        self.assertEqual(selected.fingerprint, RAVEN_SPEC_SHA)

    def test_new_cli_accepts_legacy_manager_with_no_cold_profile(self):
        raven = json.loads((ROOT / "examples/fingerprints/pixel-raven-android13.json").read_text())
        manager = SimpleNamespace(cfg=SimpleNamespace(), context=SimpleNamespace(project_root=ROOT))
        for text in (json.dumps(raven), "invalid-json"):
            client = SimpleNamespace(profile_helper_dump=lambda: {"stdout": text})
            self.assertEqual(cli_module._regeneration_base_profile(manager, client), raven)

    def test_old_binding_is_rejected_without_writing_or_relabeling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_context = SimpleNamespace(state_root=root / "old", instance_id="11111111-2222-4333-8444-555555555555",
                                          resource_tag="aaaaaaaaaaaa")
            new_context = SimpleNamespace(state_root=root / "new", instance_id="22222222-2222-4333-8444-555555555555",
                                          resource_tag="bbbbbbbbbbbb")
            lease = SimpleNamespace(transaction_id="a" * 32)
            old = gs.GoogleBindingStore(old_context, lease)
            committed = old.commit(old.pending(self.raven, "fresh"))
            before = old.path.read_bytes()
            for fresh in (False, True):
                with self.assertRaises(gs.GoogleServicesError) as error:
                    gs.transition_decision(self.bluejay, committed, freshness_known=True, fresh=fresh,
                                           legacy_actual_none=False)
                self.assertEqual(error.exception.code, "google_services_new_instance_required")
            self.assertEqual(old.path.read_bytes(), before)
            self.assertEqual(gs.transition_decision(self.bluejay, None, freshness_known=True,
                                                    fresh=True, legacy_actual_none=False)["transition"], "fresh")
            new = gs.GoogleBindingStore(new_context, lease)
            fresh = new.commit(new.pending(self.bluejay, "fresh"))
            self.assertTrue(gs.binding_matches(fresh, self.bluejay))
            self.assertFalse(gs.binding_matches(fresh, self.raven))
            self.assertEqual(old.path.read_bytes(), before)
            self.assertNotEqual(fresh["dataCompatibilitySha256"], committed["dataCompatibilitySha256"])

    def test_import_manifests_are_distinct_with_identical_components(self):
        dependency = {"release": gs.MINDTHEGAPPS_RELEASE, "metadataSha256": "a" * 64,
                      "importManifestSha256": "b" * 64, "archivePath": "payload", "memberSha256": "c" * 64}
        with patch.object(gs, "_mindthegapps_source_dependency", return_value=dependency):
            old = gs._expected_import_manifest_v2(ROOT, self.raven)
            new = gs._expected_import_manifest_v2(ROOT, self.bluejay)
        self.assertEqual(old["components"], new["components"])
        self.assertEqual(old["sourceDependencies"], new["sourceDependencies"])
        self.assertNotEqual(old["release"], new["release"])
        self.assertNotEqual(old["metadataSha256"], new["metadataSha256"])

    def test_automatic_acquisition_passes_selected_release_to_importer(self):
        with tempfile.TemporaryDirectory() as directory:
            def download(_url, destination, **_kwargs):
                destination.write_bytes(b"synthetic")
            with patch.object(gs, "_asset_set_ready", side_effect=(False, False, True, False)), \
                 patch.object(gs, "load_release_spec", return_value=self.raven), \
                 patch.object(gs, "_download_pinned_asset", side_effect=download), \
                 patch.object(gs, "import_microg") as importer, \
                 patch.object(gs, "quick_validate_assets"):
                gs.ensure_google_services_assets(Path(directory), self.bluejay)
            self.assertEqual(importer.call_args.kwargs["release"], gs.MICROG_BLUEJAY_RELEASE)

    def test_import_rejects_unknown_release_before_touching_assets(self):
        with patch.object(gs, "load_release_spec") as loader:
            with self.assertRaises(gs.GoogleServicesError):
                gs.import_microg(ROOT, Path(gs.MICROG_GMSCORE_BASENAME), Path(gs.MICROG_GSFPROXY_BASENAME),
                                 release="unregistered-bluejay")
            loader.assert_not_called()

    def test_resource_flags_preserve_exact_requested_caps_and_argv(self):
        args = ["--cpus", "1", "--memory", "2147483648", "--memory-swap", "2684354560"]
        before = args.copy()
        self.assertEqual(gs.parse_google_resource_args(args),
                         {"NanoCpus": 1000000000, "Memory": 2147483648, "MemorySwap": 2684354560})
        self.assertEqual(args, before)
        equivalent = ["--cpus=1.0", "--memory=2g", "--memory-swap=2.5GB"]
        self.assertEqual(gs.parse_google_resource_args(equivalent), gs.parse_google_resource_args(args))
        context = SimpleNamespace(project_root=ROOT)
        for release, profile in ((gs.MICROG_PLAY_RELEASE, None), (gs.MICROG_BLUEJAY_RELEASE, self.profile)):
            cfg = self.cfg(release, profile); cfg.extra_docker_args = args
            resolved = gs.resolve_google_runtime_spec(context, cfg, "runtime-image-build", require_assets=False)
            self.assertEqual(resolved.release, release)
            self.assertEqual(cfg.extra_docker_args, before)

    def test_resource_only_parser_rejects_other_docker_controls_and_duplicates(self):
        invalid = (["--network=host"], ["--entrypoint", "sh"], ["--env", "X=Y"], ["--volume=/x:/data"],
                   ["--gpus=all"], ["--privileged"], ["-m", "2g"], ["--cpus"], ["--cpus="],
                   ["--cpus", "NaN"], ["--cpus", "inf"], ["--cpus", "-1"], ["--cpus", "0.0000000001"],
                   ["--cpus", "1e2"], ["--cpus=1", "--cpus", "2"], ["--memory", "2g", "--memory=3g"],
                   ["--memory", "2gi"], ["--memory", "1.1b"], ["--memory", "-1"],
                   ["--memory", str(2**63)], ["--memory-swap", "3g"],
                   ["--memory", "2g", "--memory-swap", "1g"], ["--memory=2g --entrypoint=sh"],
                   ["--cpus", True], "--cpus=1")
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(gs.GoogleServicesError) as error:
                gs.parse_google_resource_args(args)
            self.assertEqual(error.exception.code, "google_services_container_customization_unsupported")
        raven = self.cfg(gs.MICROG_PLAY_RELEASE)
        raven.extra_docker_args = ["--network=host"]
        with self.assertRaises(gs.GoogleServicesError):
            gs.resolve_google_runtime_spec(SimpleNamespace(project_root=ROOT), raven,
                                           "runtime-image-build", require_assets=False)

    def test_resource_defaults_and_unlimited_swap_follow_docker_semantics(self):
        self.assertEqual(gs.parse_google_resource_args([]), {})
        self.assertEqual(gs.parse_google_resource_args(["--cpus=0", "--memory=0"]),
                         {"NanoCpus": 0, "Memory": 0})
        self.assertEqual(gs.parse_google_resource_args(["--memory=2g", "--memory-swap=-1"]),
                         {"Memory": 2147483648, "MemorySwap": -1})


if __name__ == "__main__":
    unittest.main()
