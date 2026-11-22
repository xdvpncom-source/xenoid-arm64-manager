"""Bounded display generation, frozen legacy compatibility and official proof."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import qa_device_profile as q
import qa_profile_runtime as runtime

class DisplayRandomTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((Path(__file__).parent / 'fixtures/native-profile-template.json').read_text())
        self.resources = {'enabled': True, 'fps': 20, 'scale': 'minimum', 'min_side': 200, 'dpi': 72, 'physical_size': [720, 1280]}
        self.policy = q.default_policy([self.base], self.resources)
        self.previous = q.normalize_resources(self.base, self.resources)
    def generate(self, key='bounded-display', previous=None):
        return q.generate_profile(self.base, self.policy, key, previous=self.previous if previous is None else previous, resource_config=self.resources)
    def test_defaults_cover_below_and_above_current_with_scaled_dpi(self):
        self.assertNotIn('display_presets', self.policy)
        self.assertEqual(self.policy['display_random'], q.DEFAULT_DISPLAY_RANDOM)
        candidates = q.display_random_candidates(self.policy['display_random'])
        self.assertEqual(len(candidates), 11)
        self.assertEqual(candidates[0], {'width': 200, 'height': 356, 'dpi': 72, 'fps': 20})
        self.assertEqual(candidates[-1], {'width': 220, 'height': 392, 'dpi': 79, 'fps': 20})
        self.assertTrue(all(v['height'] % 2 == 0 for v in candidates))
    def test_seeded_choice_is_replayable_avoids_previous_and_uses_whole_range(self):
        previous = self.previous; seen = set()
        for index in range(55):
            first = self.generate('range-' + str(index), previous)
            self.assertEqual(first, self.generate('range-' + str(index), previous))
            p = first['profile']; w, h = p['display']['width'], p['display']['height']
            self.assertNotEqual((w, h), (previous['display']['width'], previous['display']['height']))
            self.assertEqual(first['display_source'], 'random')
            self.assertEqual(p['input']['x'], {'minimum': 0, 'maximum': w - 1})
            self.assertEqual(p['input']['y'], {'minimum': 0, 'maximum': h - 1})
            self.assertEqual(p['display']['defaultRefreshRateHz'], 20)
            self.assertLessEqual(abs(h - w * 356 / 200), 1)
            seen.add(w); previous = p
        self.assertIn(200, seen); self.assertTrue(any(w > 200 for w in seen)); self.assertGreater(len(seen), 8)
    def test_uniform_selector_does_not_always_favor_a_range_edge(self):
        candidates = q.display_random_candidates(self.policy['display_random']); counts = {row['width']: 0 for row in candidates}
        for index in range(2100):
            selected = q._uniform_choice(hashlib.sha256(str(index).encode()).digest(), candidates); counts[selected['width']] += 1
        self.assertTrue(all(140 < value < 250 for value in counts.values()), counts)
    def test_rounding_and_unaligned_upper_bound_are_explicit(self):
        config = dict(q.DEFAULT_DISPLAY_RANDOM, base_width=400, base_height=600, base_dpi=73, min_width=201, max_width=206, width_step=2)
        candidates = q.display_random_candidates(config)
        self.assertEqual([p['width'] for p in candidates], [201, 203, 205])
        self.assertEqual(candidates[0], {'width': 201, 'height': 302, 'dpi': 72, 'fps': 20})
        self.assertEqual(candidates[1]['height'], 304)
        self.assertEqual(candidates[2]['height'], 308)
        self.assertEqual(q.display_random_candidates(dict(config, min_width=202, max_width=202))[0]['height'], 304)

    def test_frozen_group_retains_current_and_scalar_choices_remain_inactive(self):
        self.policy['display_random']['enabled'] = False; self.policy['fields']['display.width']['values'] = [1000]
        before = copy.deepcopy(self.previous); got = self.generate()['profile']
        self.assertEqual(got['display'], before['display']); self.assertEqual(got['input'], before['input'])
        self.assertEqual(self.generate()['display_source'], 'frozen')
        self.assertFalse(set(q.DISPLAY_PRESET_FIELDS) & set(q.selected_fields(self.base, self.policy)))
        managed = [r for r in q.field_catalog(self.base, self.policy) if r.get('managed_by')]
        self.assertEqual({r['path'] for r in managed}, q.DISPLAY_RANDOM_MANAGED)
        self.assertTrue(all(r['managed_by'] == 'display_random' and not r['mutable'] for r in managed))
    def test_legacy_and_saved_presets_do_not_migrate_on_read(self):
        legacy = copy.deepcopy(self.policy); legacy.pop('display_random'); legacy['revision'] = 3
        self.assertEqual(q.validate_policy(legacy, [self.base]), legacy)
        migrated = q.migrate_policy(legacy, [self.base], self.resources)
        self.assertEqual(migrated['revision'], 4); self.assertEqual(migrated['display_random'], q.DEFAULT_DISPLAY_RANDOM)
        legacy['display_presets'] = {'enabled': False, 'values': copy.deepcopy(q.DEFAULT_DISPLAY_PRESETS)}
        migrated = q.migrate_policy(legacy, [self.base], self.resources)
        self.assertNotIn('display_random', migrated); self.assertEqual(migrated['display_presets'], legacy['display_presets'])
        self.assertEqual(q.migrate_policy(self.policy, [self.base])['display_random'], self.policy['display_random'])
    def test_invalid_full_ranges_and_conflicting_groups_are_rejected(self):
        cases = [dict(base_width=0), dict(base_height=1), dict(min_width=199), dict(max_width=40000), dict(width_step=0), dict(width_step=True), dict(fps=0), dict(base_dpi=71), dict(min_width=220, max_width=200), dict(base_width=2, base_height=32767), dict(base_width=400, base_height=200, min_width=200, max_width=220), dict(base_width=2, base_dpi=65535), dict(unrecognized=1)]
        for change in cases:
            candidate = copy.deepcopy(self.policy); candidate['display_random'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError): q.validate_policy(candidate, [self.base])
        conflict = copy.deepcopy(self.policy); conflict['display_presets'] = {'enabled': False, 'values': []}
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'): q.validate_policy(conflict, [self.base])

    def test_historical_profile_smaller_than_wm_floor_remains_readable(self):
        historical = copy.deepcopy(self.previous)
        historical['display'].update(width=199, height=199)
        historical['input']['x'] = {'minimum': 0, 'maximum': 198}
        historical['input']['y'] = {'minimum': 0, 'maximum': 198}
        self.assertEqual(q.validate_profile(historical), historical)

class DisplayRandomRuntimeTests(unittest.TestCase):
    def fixture(self):
        import test_qa_profile_runtime as fixture
        value = fixture.RuntimeTest(); value.setUp(); self.addCleanup(value.doCleanups)
        value.policy['display_random'] = copy.deepcopy(q.DEFAULT_DISPLAY_RANDOM)
        for row in q.field_catalog(value.base):
            if row['mutable']: value.policy['fields'][row['path']]['enabled'] = False
        self.capture(value); return value
    def capture(self, value):
        value.policy_file.write_text(json.dumps(value.policy))
        value.receipt['profile_plan'] = runtime.capture_plan(value.broker, value.manager, value.manager.binding, None, config_path=value.policy_file, resource_path=value.resource_file)
    def test_real_consumer_and_native_geometry_proof_required_and_retry_pins_range(self):
        value = self.fixture(); value.broker.bad_display_input = True
        with self.assertRaisesRegex(runtime.ProfilePending, 'native_display_geometry_pending'): value.complete()
        target = value.receipt['profile_progress']['target_sha256']
        self.assertIsNone(runtime.load_applied(value.manager.context.instance_id, state_dir=value.root / 'state'))
        value.policy['display_random'].update(min_width=280, max_width=320); value.policy_file.write_text(json.dumps(value.policy))
        value.broker.bad_display_input = False; result = value.complete()
        self.assertEqual(result['profile_sha256'], target); self.assertEqual(result['display_source'], 'random')
        self.assertLessEqual(value.manager.profile['display']['width'], 220); self.assertNotEqual(value.manager.profile['display']['width'], 200)
        self.assertTrue(result['display_input']['ok']); self.assertFalse(result['display_input']['events_injected'])
        self.assertEqual(value.manager.actual_reloads, 1); self.assertTrue(result['effective_changes_verified'])
    def test_bad_endpoint_is_rejected_during_pre_gen_capture(self):
        value = self.fixture(); value.policy['display_random'].update(base_width=2, base_height=32767)
        value.policy_file.write_text(json.dumps(value.policy))
        with self.assertRaises(ValueError): self.capture(value)
        self.assertEqual(value.cli.apply_calls, 0); self.assertEqual(value.manager.actual_reloads, 0)

    def test_wm_clamped_candidates_are_rejected_before_native_gen(self):
        for mode in ('range', 'presets', 'legacy'):
            value = self.fixture()
            if mode == 'range':
                value.policy['display_random']['min_width'] = 199
            else:
                value.policy.pop('display_random')
                if mode == 'presets':
                    value.policy['display_presets'] = {'enabled': True, 'values': [
                        {'width': 200, 'height': 199, 'dpi': 72, 'fps': 20}]}
                else:
                    value.policy['fields']['display.width'] = {'enabled': True, 'values': [199]}
            with self.subTest(mode=mode), self.assertRaises(ValueError): self.capture(value)
            self.assertEqual(value.cli.apply_calls, 0)
            self.assertEqual(value.manager.actual_reloads, 0)
    def test_fixed_single_width_and_disabled_range_are_intentional_noops_with_proof(self):
        for enabled in (True, False):
            value = self.fixture(); value.policy['display_random'].update(enabled=enabled, min_width=200, max_width=200); self.capture(value)
            result = value.complete(); self.assertEqual(value.manager.profile['display']['width'], 200)
            self.assertEqual(result['randomization_skipped'], 'fixed_display_only' if enabled else 'no_enabled_fields')
            self.assertTrue(result['display_input']['ok']); self.assertFalse(result['display_input']['events_injected'])

if __name__ == '__main__': unittest.main()
