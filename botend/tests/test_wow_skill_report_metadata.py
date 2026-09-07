from unittest.mock import patch, Mock

from django.test import TestCase, SimpleTestCase

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion, WowSkillDiffReport
from botend.services.wow_skill_report_metadata import (
    build_report_spell_metadata, database_spell_metadata, report_spell_entries, wowhead_spell_url,
)


class ReportDatabaseMetadataTests(TestCase):
    def test_existing_report_metadata_uses_saved_content_and_ignores_client_spell_ids(self):
        report = WowSkillDiffReport.objects.create(branch='wowt', from_build='12.1.0.69497', to_build='12.1.0.69587', content_html_path='portal/reports/test.html')
        path = Mock()
        path.read_text.return_value = '已保存的报告正文'
        with patch('botend.portal.views._resolve_portal_report_html_path', return_value=path), \
             patch('botend.portal.views.build_report_spell_metadata', return_value={'2098': {'name': '斩击'}}) as build:
            response = self.client.get(f'/portal/api/wow-skill-diff/{report.id}/metadata/?spell_ids=999999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['spells']['2098']['name'], '斩击')
        build.assert_called_once_with('已保存的报告正文', 'wowt', '12.1.0.69587')
        self.assertEqual(self.client.get('/portal/api/wow-skill-diff/999999/metadata/').status_code, 404)

    def test_reuses_other_locale_and_talents_without_wrong_branch_or_future_build(self):
        build = '12.1.0.69587'
        WowSpellSnapshot.objects.create(branch='wowt', locale='zhCN', spell_id=2098, snapshot_build=build, name='斩击', icon='ability_rogue_waylay')
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=383103, snapshot_build=build, icon='wrong_retail')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=101, snapshot_build=build, name='其他分支名称', icon='shared_icon')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=102, snapshot_build=build, icon='conflict_a')
        WowSpellSnapshot.objects.create(branch='wow_beta', locale='zhCN', spell_id=102, snapshot_build=build, icon='conflict_b')
        old = WowTalentVersion.objects.create(key='old', branch='ptr', current_build='12.1.0.69283')
        future = WowTalentVersion.objects.create(key='future', branch='ptr', current_build='12.1.0.69999')
        WowTalentNodeMetadata.objects.create(talent_version=old, spell_id=383103, display_spell_id=383103, name='残暴动力', icon='ability_demonhunter_bloodlet')
        WowTalentNodeMetadata.objects.create(talent_version=future, spell_id=383103, icon='wrong_future')
        WowTalentNodeMetadata.objects.create(talent_version=old, spell_id=99, display_spell_id=100, icon='display_only')
        result = database_spell_metadata([2098, 383103, 99, 100, 101, 102], 'wowt', build)
        self.assertEqual(result[2098]['icon'], 'ability_rogue_waylay')
        self.assertEqual(result[2098]['name'], '斩击')
        self.assertEqual(result[383103]['icon_source'], 'talent_metadata')
        self.assertEqual(result[383103]['icon'], 'ability_demonhunter_bloodlet')
        self.assertEqual(result[99]['icon'], '')
        self.assertEqual(result[100]['icon'], 'display_only')
        self.assertEqual(result[383103]['name'], '')
        self.assertEqual(result[101]['icon'], 'shared_icon')
        self.assertEqual(result[101]['icon_source'], 'database_shared')
        self.assertEqual(result[101]['name'], '')
        self.assertEqual(result[102]['icon'], '')


class ReportRelationshipTests(SimpleTestCase):
    html = """<article class='spell' id='spell-1256919'><span class='spell-title'>武器战士</span>
    <span class='impact-evidence'>应用光环(#8)</span><div class='line'>应用光环(#8)</div></article>
    <article class='spell' id='spell-2098'><span class='spell-title'>斩击</span></article>"""

    def test_only_changed_effect_is_resolved_and_database_icons_do_not_fetch_tooltips(self):
        def db2(table, build, field, value, locale='enUS'):
            self.assertEqual(build, '12.1.0.69587')
            if table == 'SpellEffect':
                return [
                    {'EffectIndex': '8', 'EffectAura': '649', 'EffectMiscValue_1': '3450'},
                    {'EffectIndex': '9', 'EffectAura': '648', 'EffectMiscValue_1': '2670'},
                ]
            if table == 'SpellLabel':
                self.assertEqual(value, 3450)
                return [{'SpellID': '445579'}]
            return [{'Name_lang': '屠戮者打击'}]

        def local(ids, branch, build):
            return {sid: {'name': '', 'icon': 'existing_icon', 'icon_source': 'talent_metadata'} for sid in ids}

        with patch('botend.services.wow_skill_report_metadata._db2_rows', side_effect=db2), \
             patch('botend.services.wow_skill_report_metadata.database_spell_metadata', side_effect=local), \
             patch('botend.services.wow_skill_report_metadata._tooltip_icon') as tooltip:
            result = build_report_spell_metadata(self.html, 'wowt', '12.1.0.69587')
        tooltip.assert_not_called()
        effects = result['1256919']['effects']
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['targets'][0]['name'], '屠戮者打击')
        self.assertEqual(effects[0]['targets'][0]['icon_source'], 'talent_metadata')
        self.assertIn('/ptr/spell=445579', effects[0]['targets'][0]['url'])
        self.assertEqual(result['2098']['name'], '斩击')

    def test_missing_metadata_retains_source_and_no_invented_targets(self):
        with patch('botend.services.wow_skill_report_metadata._db2_rows', return_value=[]), \
             patch('botend.services.wow_skill_report_metadata.database_spell_metadata', return_value={sid: {'name': '', 'icon': '', 'icon_source': ''} for sid in (1256919, 2098)}), \
             patch('botend.services.wow_skill_report_metadata._tooltip_icon', return_value=''):
            result = build_report_spell_metadata(self.html, 'wowt', '12.1.0.69587')
        self.assertEqual(result['1256919']['name'], '武器战士')
        self.assertEqual(result['1256919']['effects'], [])
        self.assertEqual(result['1256919']['icon_url'], '')

    def test_pvp_class_mask_resolves_only_matching_family_bits(self):
        def db2(table, build, field, value, locale='enUS'):
            if table == 'SpellEffect':
                return [{'EffectIndex': '8', 'EffectAura': '647', 'EffectSpellClassMask_0': '32'}]
            if table == 'SpellClassOptions' and field == 'SpellID':
                return [{'SpellClassSet': '8'}]
            if table == 'SpellClassOptions':
                self.assertEqual((field, value), ('SpellClassSet', 8))
                return [{'SpellID': '100', 'SpellClassMask_0': '32'}, {'SpellID': '101', 'SpellClassMask_0': '64'}]
            return [{'Name_lang': '匹配的技能'}]

        with patch('botend.services.wow_skill_report_metadata._db2_rows', side_effect=db2), \
             patch('botend.services.wow_skill_report_metadata.database_spell_metadata', side_effect=lambda ids, *args: {sid: {'icon': 'existing', 'name': '', 'icon_source': 'spell_snapshot'} for sid in ids}):
            result = build_report_spell_metadata(self.html, 'wowt', '12.1.0.69587')
        relation = result['1256919']['effects'][0]
        self.assertEqual(relation['aura'], 647)
        self.assertEqual(relation['spell_ids'], [100])

    def test_branch_links_and_saved_report_effect_indices(self):
        self.assertEqual(report_spell_entries(self.html)[1256919]['indices'], {8})
        for branch, prefix in [('wow', ''), ('wowt', 'ptr/'), ('wowxptr', 'ptr-2/'), ('wow_beta', 'beta/')]:
            self.assertEqual(wowhead_spell_url(branch, 1), f'https://www.wowhead.com/{prefix}spell=1')
        self.assertEqual(build_report_spell_metadata(self.html, 'wowt', 'invalid'), {})
