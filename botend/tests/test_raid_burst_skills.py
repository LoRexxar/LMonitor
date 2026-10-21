"""全专精技能身份、事件归属和替换编号回归。"""
from copy import deepcopy
from django.test import SimpleTestCase
from botend.constants.wow import CLASS_SPEC_MAP, SPEC_IDENTITY_MAP
from botend.services.raid_burst_skills import SUPPORTED
from botend.services.raid_burst_aggregation import normalize


class RaidBurstSkillTests(SimpleTestCase):
    def raw(self, config):
        base = {'sourceID': 7, 'targetID': 7, 'fight': 3}
        casts = [dict(base, type='cast', timestamp=12000 + i * 1000, abilityGameID=int(spell))
                 for i, spell in enumerate(config['spells'])]
        return {'report': {'fights': [{'id': 3, 'startTime': 10000, 'endTime': 310000}]},
                'actor': {'id': 7, 'name': '技能测试玩家', 'server': '测试服'},
                'streams': {'combatants': {'complete': True, 'data': [dict(base, specID=config['spec_id'], auras=[])]},
                            'buffs': {'complete': True, 'data': [dict(base, type='applybuff', timestamp=10000, abilityGameID=2825)]},
                            'casts': {'complete': True, 'data': casts}}}

    def normalize(self, raw, config):
        return normalize(raw, {'fight_id': 3, 'realm': '测试服', 'region': 'CN',
                               'dps': 12345, 'report_code': 'ABC'}, 1, config)

    def test_every_spec_has_its_own_identity_and_successful_casts(self):
        self.assertEqual(set(SUPPORTED), {(cls, spec) for cls, specs in CLASS_SPEC_MAP.items() for spec in specs})
        for pair, config in SUPPORTED.items():
            with self.subTest(pair=pair):
                self.assertEqual(SPEC_IDENTITY_MAP[config['spec_id']], pair)
                self.assertTrue(config['spells'])
                sample, reason = self.normalize(self.raw(config), config)
                self.assertIsNone(reason)
                self.assertEqual(sample['times'], {spell: [2 + i] for i, spell in enumerate(config['spells'])})

    def test_only_own_successful_casts_in_selected_fight_are_used(self):
        for config in SUPPORTED.values():
            raw = self.raw(config)
            cast = raw['streams']['casts']['data'][0]
            for changes in ({'sourceID': 8}, {'fight': 4}, {'fake': True},
                            {'type': 'applybuff'}, {'type': 'damage'}, {'type': 'begincast'},
                            {'timestamp': 9000}, {'timestamp': 400000}):
                raw['streams']['casts']['data'].append(dict(cast, timestamp=20000, **changes)
                                                      if 'timestamp' not in changes else dict(cast, **changes))
            sample, _ = self.normalize(raw, config)
            self.assertEqual(sample['times'][str(cast['abilityGameID'])], [2])
            wrong = deepcopy(raw)
            wrong['streams']['combatants']['data'][0]['specID'] = -1
            self.assertEqual(self.normalize(wrong, config), (None, 'invalid'))

    def test_aliases_merge_once_without_duplicate_buttons(self):
        for config in SUPPORTED.values():
            for alias, canonical in config['cast_aliases'].items():
                with self.subTest(spec=config['spec_id'], alias=alias):
                    raw = self.raw(config)
                    event = next(e for e in raw['streams']['casts']['data'] if str(e['abilityGameID']) == canonical)
                    raw['streams']['casts']['data'].append(dict(event, abilityGameID=int(alias)))
                    sample, _ = self.normalize(raw, config)
                    self.assertEqual(len(sample['times'][canonical]), 1)
                    self.assertNotIn(alias, sample['times'])
