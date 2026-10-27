"""全专精技能身份、事件归属和替换编号回归。"""
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from botend.services import raid_burst_aggregation as agg
from botend.services.raid_burst_snapshot import read_blob
from django.test import SimpleTestCase
from botend.constants.wow import CLASS_SPEC_MAP, SPEC_IDENTITY_MAP
from botend.services.raid_burst_skills import SUPPORTED
from botend.services.raid_burst_aggregation import normalize


class RaidBurstSkillTests(SimpleTestCase):
    def raw(self, config):
        base = {'sourceID': 7, 'targetID': 7, 'fight': 3}
        auras = config.get('aura_applications', {})
        casts = [dict(base, type='cast', timestamp=12000 + i * 1000, abilityGameID=int(spell))
                 for i, spell in enumerate(config['spells']) if spell not in auras]
        applications = [dict(base, type='applybuff', timestamp=12000 + i * 1000,
                             abilityGameID=auras[spell])
                        for i, spell in enumerate(config['spells']) if spell in auras]
        return {'report': {'fights': [{'id': 3, 'startTime': 10000, 'endTime': 310000}]},
                'actor': {'id': 7, 'name': '技能测试玩家', 'server': '测试服'},
                'streams': {'combatants': {'complete': True, 'data': [dict(base, specID=config['spec_id'], auras=[])]},
                            'buffs': {'complete': True, 'data': [dict(base, type='applybuff', timestamp=10000, abilityGameID=2825)] + applications},
                            'casts': {'complete': True, 'data': casts}}}

    def normalize(self, raw, config):
        return normalize(raw, {'fight_id': 3, 'realm': '测试服', 'region': 'CN',
                               'dps': 12345, 'report_code': 'ABC'}, 1, config)

    def test_every_spec_has_its_own_identity_and_configured_event_source(self):
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
            self.assertEqual(sample['times'][str(cast['abilityGameID'])], [(cast['timestamp'] - 10000) / 1000])
            wrong = deepcopy(raw)
            wrong['streams']['combatants']['data'][0]['specID'] = -1
            self.assertEqual(self.normalize(wrong, config), (None, 'invalid'))

    def test_devourer_requires_confirmed_self_aura_applications(self):
        config = SUPPORTED[SPEC_IDENTITY_MAP[1480]]
        raw = self.raw(config)
        # Model the captured WCL contract: no 1217605 cast, only 1217607 apply.
        raw['streams']['casts']['data'] = [dict(type='cast', sourceID=7, targetID=99,
            fight=3, timestamp=14000, abilityGameID=1221150)]
        raw['streams']['buffs']['data'] = raw['streams']['buffs']['data'][:1]
        event = dict(type='applybuff', sourceID=7, targetID=7, fight=3,
                     timestamp=21125, abilityGameID=1217607)
        raw['streams']['buffs']['data'] += [event, dict(event), dict(event, timestamp=86698)]
        sample, reason = self.normalize(raw, config)
        self.assertIsNone(reason)
        self.assertEqual(sample['times'], {'1217605': [11.125, 76.698], '1221150': [4]})
        self.assertEqual(sample['lust_focus']['1217605'], [11.125])
        self.assertEqual(config['aura_applications'], {'1217605': 1217607})
        # The boundary is inclusive, but never synthesize an activation from an
        # initial aura or use a cast/damage/resource stack as aura evidence.
        invalid = [dict(event, **changes) for changes in (
            {'type': kind} for kind in ('refreshbuff', 'applybuffstack', 'removebuffstack',
                                       'removebuff', 'cast', 'begincast', 'damage'))]
        for field, values in (
            ('sourceID', (8, None, 0, -1, '7', True, 7.0)),
            ('targetID', (8, None, 0, -1, '7', True, 7.0)),
            ('fight', (4, None, '3', 3.0)),
            ('timestamp', (9999, 310001)),
            ('abilityGameID', (1225789, 1217605, 1221150)),
            ('fake', (True,)),
        ):
            invalid.extend(dict(event, **{field: value}) for value in values)
        for field in ('sourceID', 'targetID', 'fight', 'timestamp'):
            missing = dict(event)
            missing.pop(field)
            invalid.append(missing)
        for rejected in invalid:
            with self.subTest(event=rejected):
                candidate = deepcopy(raw)
                candidate['streams']['buffs']['data'] = raw['streams']['buffs']['data'][:1] + [rejected]
                candidate['streams']['combatants']['data'][0]['auras'] = [
                    {'ability': 1217607, 'source': 7}, {'ability': 1225789, 'source': 7}]
                # Neither a canonical cast nor a buff misplaced in Casts counts.
                candidate['streams']['casts']['data'] += [dict(event, type='cast', abilityGameID=1217605), event]
                sample, reason = self.normalize(candidate, config)
                self.assertIsNone(reason)
                self.assertEqual(sample['times']['1217605'], [])
                self.assertEqual(sample['times']['1221150'], [4])
        raw['streams']['buffs']['data'] = raw['streams']['buffs']['data'][:1] + [
            dict(event, timestamp=t) for t in (310000, 10000)]
        self.assertEqual(self.normalize(raw, config)[0]['times']['1217605'], [0, 300])
        raw['streams']['buffs']['complete'] = False
        with self.assertRaises(ValueError):
            self.normalize(raw, config)

    def test_source_metadata_publication_and_config_only_invalidation(self):
        config = SUPPORTED[SPEC_IDENTITY_MAP[1480]]
        legacy = deepcopy(config)
        legacy.pop('aura_applications', None)
        row = dict(report_code='ABC', fight_id=3, realm='测试服', region='CN',
                   character_name='技能测试玩家', class_name='DemonHunter', spec_name='Devourer',
                   boss_id=3379, difficulty=5, dps=12345)
        scope = dict(season_id=1, class_name='DemonHunter', spec_name='Devourer',
                     key='1/DemonHunter/Devourer/5/3379', boss_id=3379, difficulty=5)
        raw = self.raw(config)
        with TemporaryDirectory() as temp, patch.object(agg, 'fetch_events', return_value=raw):
            root = Path(temp)
            directory = root / '1/DemonHunter/Devourer'
            before = agg.build_scope([row], scope, legacy, root, nullcontext, None)
            old = read_blob(directory, before['blob'])
            after = agg.build_scope([row], scope, config, root, nullcontext, None)
            payload = read_blob(directory, after['blob'])
            self.assertNotEqual(old['input_fingerprint'], payload['input_fingerprint'])
            self.assertNotIn('skill_sources', old)
            self.assertEqual(payload['skill_sources'], agg.skill_event_sources(config))
            self.assertEqual(payload['skill_sources']['1217605']['event_type'], 'applybuff')
            self.assertEqual(payload['skill_sources']['1217605']['ability_id'], 1217607)
            self.assertEqual(payload['skill_sources']['1217605']['label'], '光环生效')
            self.assertEqual(payload['skill_sources']['1221150']['event_type'], 'cast')
            self.assertEqual(payload['samples'][0]['times']['1217605'], [2])
            self.assertEqual(payload['groups']['opener']['1217605']['windows'][0]['start'], 2)
            with patch.object(agg, 'fetch_events', side_effect=AssertionError('must reuse prepared')):
                self.assertEqual(agg.build_scope([row], scope, config, root, nullcontext, None), after)
        for other in SUPPORTED.values():
            if other['spec_id'] != 1480:
                self.assertNotIn('aura_applications', other)
                self.assertEqual(agg.skill_event_sources(other), {})

    def test_published_sources_drive_timeline_labels_and_log_links(self):
        from django.conf import settings
        from django.template.loader import render_to_string
        from playwright.sync_api import sync_playwright

        config = SUPPORTED[SPEC_IDENTITY_MAP[1480]]
        sample, _ = self.normalize(self.raw(config), config)
        payload = dict(skill_names=config['spells'], skill_sources=agg.skill_event_sources(config),
                       samples=[sample], groups=agg.build_groups([sample], config['spells']))
        html = render_to_string('portal/raid_burst.html', {'raid_burst_payload': payload})
        with sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            page = browser.new_page()
            page.set_default_timeout(3000)
            page.route('**/*', lambda route: route.abort())
            page.set_content(html)
            page.add_script_tag(path=str(Path(settings.BASE_DIR) / 'static/portal/js/raid-burst.js'))
            self.assertEqual(page.locator('#timeline-title').inner_text(), '光环生效时间轴')
            self.assertIn('applybuff', page.locator('#skill-source').inner_text())
            self.assertIn('非主动施法', page.locator('#skill-source').inner_text())
            self.assertIn('光环生效', page.locator('#chart svg > title').text_content())
            self.assertEqual(page.locator('#chart circle').count(), 1)
            page.locator('#chart [data-sample-rank]').focus()
            self.assertIn('光环生效', page.locator('#burst-sample-tooltip').inner_text())
            link = page.locator('#groups a').first.get_attribute('href')
            self.assertIn('type=auras', link)
            self.assertIn('ability=1217607', link)
            page.locator('button[data-spell="1221150"]').click()
            self.assertEqual(page.locator('#timeline-title').inner_text(), '成功施法时间轴')
            self.assertIn('成功 cast', page.locator('#skill-source').inner_text())
            self.assertIn('type=casts', page.locator('#groups a').first.get_attribute('href'))
            # Legacy published blobs without source metadata retain cast semantics.
            payload.pop('skill_sources')
            html = render_to_string('portal/raid_burst.html', {'raid_burst_payload': payload})
            page.set_content(html)
            page.add_script_tag(path=str(Path(settings.BASE_DIR) / 'static/portal/js/raid-burst.js'))
            self.assertEqual(page.locator('#timeline-title').inner_text(), '成功施法时间轴')
            self.assertIn('type=casts', page.locator('#groups a').first.get_attribute('href'))

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
