"""Offline contracts for the additive US carrier catalog.

The legacy fixture is synthetic output from the original 2026-08-08 catalog,
using seed b"a" * 32. It is not a captured subscriber identity.

MCC/MNC: ITU E.212 bulletin 1280 (2023), printed pages 60, 61 and 64.
APN: att.com/support/article/wireless/KM1062162/ and
verizon.com/support/knowledge-base-302135/.
LTE subset: AT&T Franklin A10 specifications and Verizon Internet Gateway
NCQ-1338 user guide, technical specifications. Only shared bands 2/4/5 were
added; this catalog does not claim the carriers have no other LTE bands.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "nativebundle/source/data/cellular/carriers.json"
SOURCE = ROOT / "nativebundle/source/src/xenoid/cellular.py"
SPEC = importlib.util.spec_from_file_location("us_catalog_cellular", SOURCE)
cellular = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cellular)

EXPECTED_CARRIERS = [
    {"apn": "fast.t-mobile.com", "bands": [2, 4, 5, 12, 66, 71],
     "mcc": "310", "mnc": "260", "name": "T-Mobile"},
    {"apn": "NXTGENPHONE", "bands": [2, 4, 5],
     "mcc": "310", "mnc": "410", "name": "AT&T Mobility"},
    {"apn": "vzwinternet", "bands": [2, 4, 5],
     "mcc": "311", "mnc": "480", "name": "Verizon Wireless"},
]
OLD_DATASET_CANONICAL_SHA256 = "2a4c3491a8628cf4d23ca3598f60963572067d2093bd4d834951d1b32b2b5373"
OLD_NON_US_PROFILE_DIGESTS = {
    "AU": "ff89f96a521467f302232227b6c43d1c6d33d8690c884b16e3709d2f6b9390b6",
    "DE": "94cd1946b37e8576e5cb3a2900c375bccffb613ad47fd6350dbe8be75ea9c884",
    "GB": "71d2184aba1ecd6ee4fcf581229452ab50be7f8edb9b218aab9033228b9ba3b8",
    "HK": "749ab348f24f2480fe2aaef82db32af0c72c606e34b8ae4c024c129f087eb5f9",
    "JP": "669c42d17fa1c3ac5f94ddeacff1c7ff1ca00799b01b7581190852d885d24770",
    "SG": "a3d63626a59326a920d97246fec2d5650ed4ecd37f0d8b3e7d4796ab2f6d073f",
}


class UsCarrierCatalogTests(unittest.TestCase):
    def test_catalog_is_additive_and_preserves_saved_dataset_version(self):
        catalog = json.loads(CATALOG.read_bytes())
        self.assertEqual(catalog["countries"]["US"]["carriers"], EXPECTED_CARRIERS)
        self.assertEqual(catalog["snapshotVersion"], "2026-08-08")
        legacy = copy.deepcopy(catalog)
        legacy["countries"]["US"]["carriers"] = legacy["countries"]["US"]["carriers"][:1]
        self.assertEqual(legacy["countries"]["US"]["msisdn"].pop("generationV2"),
                         {"kind": "us-nanp-national-pattern", "template": "+1##########"})
        encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), OLD_DATASET_CANONICAL_SHA256)

    def test_production_loader_pin_matches_catalog(self):
        self.assertEqual(cellular._DATASET_SHA256, hashlib.sha256(CATALOG.read_bytes()).hexdigest())
        self.assertEqual(cellular.dataset_countries()["US"]["carriers"], EXPECTED_CARRIERS)

    def test_each_us_carrier_is_selectable_and_generates_valid_matching_identity(self):
        seen = set()
        expected = {item["name"]: item for item in EXPECTED_CARRIERS}
        ranges = {2: (600, 1199), 4: (1950, 2399), 5: (2400, 2649),
                  12: (5010, 5179), 66: (66436, 67335), 71: (68586, 68935)}
        for number in range(96):
            seed = number.to_bytes(32, "big")
            profile = cellular.generate_cellular_profile("US", seed)
            self.assertEqual(profile, cellular.generate_cellular_profile("US", seed))
            self.assertEqual(cellular.validate_profile(profile), profile)
            carrier = expected[profile["carrier"]["name"]]
            self.assertEqual(profile["carrier"], carrier)
            numeric = carrier["mcc"] + carrier["mnc"]
            self.assertTrue(profile["sim"]["imsi"].startswith(numeric))
            self.assertEqual(len(profile["sim"]["imsi"]), 15)
            self.assertTrue(profile["sim"]["iccid"].startswith("89" + numeric))
            self.assertEqual(profile["operator"]["numeric"], numeric)
            self.assertEqual(profile["operator"]["isoCountry"], "us")
            self.assertEqual(profile["operator"]["roaming"], False)
            self.assertEqual(profile["callingCode"], "+1")
            self.assertEqual(profile["timezone"], "America/New_York")
            low, high = ranges[profile["cell"]["band"]]
            self.assertLessEqual(low, profile["cell"]["earfcn"])
            self.assertLessEqual(profile["cell"]["earfcn"], high)
            encoded = cellular.encode_profile_v1(profile)
            self.assertTrue(encoded.startswith(cellular.PROFILE_MAGIC))
            self.assertLess(len(encoded), cellular.MAX_PROFILE_BYTES)
            seen.add(carrier["name"])
        self.assertEqual(seen, set(expected))

    def test_legacy_t_mobile_profile_and_pin_remain_byte_for_byte_compatible(self):
        old = json.loads((ROOT / "tests/fixtures/us_carrier_legacy_profile.json").read_bytes())
        self.assertEqual(cellular.validate_profile(old), old)
        pin = cellular.carrier_pin(old)
        regenerated = cellular.generate_cellular_profile("US", b"a" * 32, pin=pin)
        self.assertEqual(regenerated, old)
        self.assertEqual(cellular.encode_profile_v1(regenerated), cellular.encode_profile_v1(old))

    def test_explicit_existing_carrier_pins_are_not_randomly_replaced(self):
        for pin in EXPECTED_CARRIERS:
            profiles = [cellular.generate_cellular_profile("US", bytes([n]) * 32, pin=pin)
                        for n in range(8)]
            self.assertTrue(all(profile["carrier"] == pin for profile in profiles))
            self.assertEqual(len({profile["identityDigest"] for profile in profiles}), len(profiles))

    def test_unknown_or_mismatched_carrier_pin_still_fails_closed(self):
        for field, value in (("mnc", "999"), ("apn", "other-apn"), ("bands", [2, 4, 5, 99])):
            pin = dict(EXPECTED_CARRIERS[1], **{field: value})
            with self.assertRaisesRegex(cellular.CellularError, "^carrier_pin_invalid$"):
                cellular.generate_cellular_profile("US", b"a" * 32, pin=pin)

    def test_non_us_deterministic_profiles_are_unchanged(self):
        for country, digest in OLD_NON_US_PROFILE_DIGESTS.items():
            self.assertEqual(cellular.generate_cellular_profile(country, b"a" * 32)["identityDigest"], digest)


if __name__ == "__main__":
    unittest.main()
