#!/usr/bin/env python3
"""Device names follow explicit cold templates; legacy naming/RNG stays identical."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from xenoid import cli


class DeviceNameTests(unittest.TestCase):
    def template(self):
        return json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())

    def test_legacy_absent_and_explicit_none_keep_prefix_rng_and_collision_retry(self):
        for kwargs in ({},{'cold_profile':None}):
            with self.subTest(kwargs=kwargs),patch.object(cli.secrets,'token_hex',side_effect=['abcdef','123456'])as random:
                self.assertEqual(cli._generate_device_name('Pixel 6 Pro ABCDEF',**kwargs),'Pixel 6 Pro 123456')
                self.assertEqual([call.args for call in random.call_args_list],[(3,),(3,)])

    def test_selected_bluejay_uses_template_model_and_changes_prior_name(self):
        with patch.object(cli.secrets,'token_hex',side_effect=['abcdef','123456']):
            self.assertEqual(cli._generate_device_name('Pixel 6a ABCDEF',cold_profile=self.template()),'Pixel 6a 123456')

    def test_model_comes_from_template_not_product_or_hardcoded_bluejay(self):
        profile=self.template()
        with patch('xenoid.cold_profile.selected_base_profile',return_value={'build':{'model':'QA 6a Test'}})as selected,patch.object(cli.secrets,'token_hex',return_value='abcdef'):
            self.assertEqual(cli._generate_device_name('old',cold_profile=profile),'QA 6a Test ABCDEF')
            selected.assert_called_once_with(profile,None)

    def test_invalid_selected_template_is_rejected_before_random_generation(self):
        profile=self.template();profile['build']['model']='bad\nmodel'
        with patch.object(cli.secrets,'token_hex')as random:
            with self.assertRaisesRegex(ValueError,'cold_profile_build_invalid_model'):
                cli._generate_device_name('old',cold_profile=profile)
            random.assert_not_called()


if __name__=='__main__':unittest.main()
