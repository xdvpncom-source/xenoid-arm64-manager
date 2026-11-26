"""Synthetic MSISDN generation: large US pool and exact legacy replay."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'nativebundle/source/src/xenoid/cellular.py'
spec = importlib.util.spec_from_file_location('us_msisdn_v53', PATH)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class MsisdnGenerationTests(unittest.TestCase):
    def test_legacy_implicit_and_explicit_generation_remain_exact(self):
        old = json.loads((ROOT / 'tests/fixtures/us_carrier_legacy_profile.json').read_bytes())
        pin = c.carrier_pin(old)
        implicit = c.generate_cellular_profile('US', b'a' * 32, pin=pin)
        explicit = c.generate_cellular_profile('US', b'a' * 32, pin=pin, msisdn_generation_version=1)
        self.assertEqual(implicit, old)
        self.assertEqual(explicit, old)
        self.assertEqual(c.validate_profile(old), old)
        self.assertEqual(c.encode_profile_v1(explicit), c.encode_profile_v1(old))

    def test_new_generation_is_deterministic_and_only_changes_msisdn_and_digest(self):
        seed = b'a' * 32
        old = c.generate_cellular_profile('US', seed)
        new = c.generate_cellular_profile('US', seed, msisdn_generation_version=2)
        self.assertEqual(new, c.generate_cellular_profile('US', seed, msisdn_generation_version=2))
        self.assertNotEqual(old['sim']['msisdn'], new['sim']['msisdn'])
        self.assertNotEqual(old['identityDigest'], new['identityDigest'])
        normalized = copy.deepcopy(new)
        normalized['sim']['msisdn'] = old['sim']['msisdn']
        normalized['identityDigest'] = old['identityDigest']
        self.assertEqual(normalized, old)
        self.assertEqual(c.validate_profile(new), new)
        self.assertTrue(c.encode_profile_v1(new).startswith(c.PROFILE_MAGIC))

    def test_pool_exceeds_a_billion_valid_us_numbers(self):
        pattern = re.compile(c.dataset_countries()['US']['msisdn']['nationalPattern'])
        prefixes = sum(bool(pattern.fullmatch(str(area) + str(exchange) + '0000'))
                       for area in range(200, 1000) for exchange in range(200, 1000))
        # Every matched six-digit prefix permits all 10,000 subscriber endings.
        self.assertGreater(prefixes * 10000, 1000000000)

    def test_sampling_spans_many_areas_and_has_no_small_demonstration_pool(self):
        dataset = c.dataset_countries()['US']
        numbers = [c._generate_msisdn(n.to_bytes(32, 'big'), dataset, generation_version=2)
                   for n in range(2000)]
        self.assertEqual(len(set(numbers)), 2000)
        self.assertGreater(len({number[2:5] for number in numbers}), 200)
        for number in numbers:
            self.assertRegex(number, r'^\+1[2-9][0-9]{2}[2-9][0-9]{6}$')
            self.assertRegex(number[2:], r'^(?:' + dataset['msisdn']['nationalPattern'] + r')$')

    def test_rejected_candidate_uses_a_new_deterministic_label(self):
        dataset = c.dataset_countries()['US']
        with mock.patch.object(c, '_bounded_number', side_effect=[2000000000, 2125550100]) as sampler:
            self.assertEqual(c._generate_msisdn(b'a' * 32, dataset, generation_version=2), '+12125550100')
        self.assertEqual([call.args[1] for call in sampler.call_args_list], ['msisdn-v2:0', 'msisdn-v2:1'])

    def test_invalid_or_unsupported_versions_do_not_fall_back(self):
        for version in (True, 0, 3, '2', None):
            with self.subTest(version=version), self.assertRaisesRegex(c.CellularError, 'generation_invalid'):
                c.generate_cellular_profile('US', b'a' * 32, msisdn_generation_version=version)
        with self.assertRaisesRegex(c.CellularError, 'generation_unsupported'):
            c.generate_cellular_profile('SG', b'a' * 32, msisdn_generation_version=2)

    def test_invalid_nanp_or_non_us_npa_is_still_rejected(self):
        dataset = c.dataset_countries()['US']
        for number in ('+12010000000', '+1201555000', '+120155500000', '+14165550100', '+18005550100'):
            with self.subTest(number=number), self.assertRaises(c.CellularError):
                c._validate_msisdn(number, dataset)

    def test_sampler_exhaustion_is_explicit_and_bounded(self):
        dataset = c.dataset_countries()['US']
        with mock.patch.object(c, '_bounded_number', return_value=2000000000) as sampler:
            with self.assertRaisesRegex(c.CellularError, 'generation_exhausted'):
                c._generate_msisdn(b'a' * 32, dataset, generation_version=2)
        self.assertEqual(sampler.call_count, 256)

    def test_new_profile_reloads_exactly_without_rederiving_any_id(self):
        profile = c.generate_cellular_profile('US', b'z' * 32, msisdn_generation_version=2)
        saved = c.canonical_bytes(profile)
        restored = c.validate_profile(json.loads(saved))
        self.assertEqual(c.canonical_bytes(restored), saved)
        self.assertEqual(c.encode_profile_v1(restored), c.encode_profile_v1(profile))


if __name__ == '__main__':
    unittest.main()
