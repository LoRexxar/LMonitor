from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
from io import BytesIO
import html
import json
import tempfile

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.portal.views import (
    PortalReportFileView,
    PortalWowHotfixReportView,
    PortalWowHotfixClassReportView,
    PortalWowHotfixClassMetadataAPIView,
    PortalWowSkillDiffReportView,
    _resolve_portal_report_html_path,
    portal_report_url,
)
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html
from botend.portal.api import _normalize_url as _normalize_portal_url
from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoDiffUnavailable, WagoSkillDiffMonitor


class _FakeQuerySet:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _FakeManager:
    def __init__(self, row):
        self.row = row

    def filter(self, **kwargs):
        return _FakeQuerySet(self.row)


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalWowSkillDiffReportViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _get(self, report):
        request = self.factory.get(f'/portal/wow-skill-diff/{report.id}/')
        with patch('botend.portal.views.WowSkillDiffReport.objects', _FakeManager(report)):
            return PortalWowSkillDiffReportView.as_view()(request, report_id=report.id)

    def test_missing_html_report_renders_inline_html_summary(self):
        report = SimpleNamespace(
            id=1,
            branch='wowxptr',
            from_build='12.0.7.67360',
            to_build='12.0.7.67525',
            display_from_build='',
            display_to_build='',
            content_md='# 牧师技能更新（3项）\n\n- 技能数：3\n',
            content_html_path='portal/reports/not_exists_for_test.html',
            changed_tables_json='["SpellEffect", "SpellName"]',
            spell_count=3,
            class_count=1,
            created_at=None,
        )

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('HTML 报告文件不存在，已直接用数据库中保存的报告正文生成 HTML 视图。', html)
        self.assertIn('wow-skill-diff-fallback-html', html)
        self.assertIn('SpellEffect', html)
        self.assertNotIn('wow-skill-diff-md', html)
        self.assertNotIn('marked.min.js', html)
        self.assertNotIn('<iframe', html)

    def test_existing_html_report_embeds_report_body_without_iframe(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        base_dir = Path(tmpdir.name)
        report_dir = base_dir / 'static' / 'portal' / 'reports'
        report_dir.mkdir(parents=True)
        (report_dir / 'wow_skill_diff_wowt_enUS_12_0_5_67235.html').write_text(
            '<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body><h1>PTR(测试服) 职业技能变更报告</h1><div class="spell">ok</div></body></html>',
            encoding='utf-8',
        )
        report = SimpleNamespace(
            id=2,
            branch='wowt',
            from_build='12.0.5.67186',
            to_build='12.0.5.67235',
            display_from_build='',
            display_to_build='',
            content_md='# PTR(测试服) 职业技能变更报告\n',
            content_html_path='portal/reports/wow_skill_diff_wowt_enUS_12_0_5_67235.html',
            changed_tables_json='[]',
            spell_count=0,
            class_count=0,
            created_at=None,
        )

        with override_settings(BASE_DIR=str(base_dir)):
            response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('wow-skill-diff-embedded-html', html)
        self.assertIn('PTR(测试服) 职业技能变更报告', html)
        self.assertIn('portal/js/wow-skill-diff-report.js', html)
        self.assertNotIn('<iframe', html)
        self.assertNotIn('HTML 报告文件不存在', html)

    def test_inline_html_summary_escapes_report_values(self):
        report = SimpleNamespace(
            id=3,
            branch='wowt',
            from_build='<script>alert(1)</script>',
            to_build='12.0.5.67235',
            display_from_build='',
            display_to_build='',
            content_md='# <img src=x onerror=alert(1)>\n\n- 技能数：3\n',
            content_html_path='',
            changed_tables_json='["Spell<script>"]',
            spell_count=3,
            class_count=1,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='<b>bad</b>', server_title='PTR')

        self.assertIn('&lt;b&gt;bad&lt;/b&gt;', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('Spell&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('<img src=x', html)

    def test_inline_html_summary_wraps_long_table_names(self):
        report = SimpleNamespace(
            id=4,
            branch='wowt',
            from_build='12.1.0.68209',
            to_build='12.1.0.68301',
            display_from_build='',
            display_to_build='',
            content_md='# PTR(测试服) 职业技能变更报告\n',
            content_html_path='',
            changed_tables_json='["collectablesourcevendorsparse", "creaturedisplayinfogeosetdata"]',
            spell_count=117,
            class_count=12,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='', server_title='PTR')

        self.assertIn('collectablesourcevendorsparse', html)
        self.assertIn('creaturedisplayinfogeosetdata', html)
        self.assertIn('overflow-wrap:anywhere', html)
        self.assertIn('word-break:break-word', html)
        self.assertIn('minmax(220px,1fr)', html)

    def test_inline_html_summary_renders_saved_spell_changes(self):
        report = SimpleNamespace(
            id=5,
            branch='wowt',
            from_build='12.1.0.68209',
            to_build='12.1.0.68301',
            display_from_build='',
            display_to_build='',
            content_md=(
                '# PTR(测试服) 职业技能变更报告：12.1.0.68209 → 12.1.0.68301\n'
                '- 技能数：2\n'
                '- 职业数：1\n\n'
                '## 战士 （职业 1）\n\n'
                '### 通用 （专精 0）\n\n'
                '无视苦痛(1277297) ：\n\n'
                '# 应用光环（攻强系数： 16 → 20 ）\n\n'
                'Warrior Fury 12.1 Class Set 2pc(1296645) ：Raging Blow damage increased.\n\n'
                '技能名称 名称： Old Name → New Name\n'
            ),
            content_html_path='',
            changed_tables_json='["SpellEffect", "SpellName"]',
            spell_count=2,
            class_count=1,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='', server_title='PTR')

        self.assertIn('技能变更内容', html)
        self.assertIn('class-section', html)
        self.assertIn('战士 （职业 1）', html)
        self.assertIn('通用 （专精 0）', html)
        self.assertIn('无视苦痛', html)
        self.assertIn('#1277297', html)
        self.assertIn('应用光环', html)
        self.assertIn("<span class='diff-old'>16</span>", html)
        self.assertIn("<span class='diff-new'>20</span>", html)
        self.assertIn("<span class='change-kind'>应用光环</span>", html)
        self.assertIn('Warrior Fury 12.1 Class Set 2pc', html)
        self.assertIn('#1296645', html)
        self.assertIn('Raging Blow 伤害提高.', html)
        self.assertIn('技能名称 名称', html)

    def test_inline_diff_keeps_empty_old_field_label_uncolored(self):
        report = SimpleNamespace(
            id=6,
            branch='wowt',
            from_build='12.1.0.68209',
            to_build='12.1.0.68301',
            display_from_build='',
            display_to_build='',
            content_md=(
                '# PTR(测试服) 职业技能变更报告：12.1.0.68209 → 12.1.0.68301\n\n'
                '## 猎人 （职业 3）\n\n'
                '### 通用 （专精 0）\n\n'
                '凶暴野兽(1308188) ：\n\n'
                '技能杂项 施法时间索引： → 1\n'
            ),
            content_html_path='',
            changed_tables_json='[]',
            spell_count=1,
            class_count=1,
        )

        html = build_wow_skill_diff_fallback_html(report, page_title='', server_title='PTR')

        self.assertIn('技能杂项 施法时间索引：', html)
        self.assertNotIn("<span class='diff-old'>技能杂项 施法时间索引：</span>", html)
        self.assertIn("<span class='diff-old empty'>空</span>", html)
        self.assertIn("<span class='diff-new'>1</span>", html)


class WagoSkillDiffHtmlReportTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)
        metadata_patch = patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.database_spell_metadata', return_value={})
        self.database_metadata = metadata_patch.start()
        self.addCleanup(metadata_patch.stop)

    def test_report_change_tone_marks_direct_buff_nerf_and_uncertain_fields(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

        self.assertEqual(
            monitor._report_change_tone('PvpMultiplier', '1.35', '1.485'),
            ('buff', '增强', '+10%'),
        )
        self.assertEqual(
            monitor._report_change_tone('PvpMultiplier', '1', '0.7'),
            ('nerf', '削弱', '-30%'),
        )
        self.assertEqual(
            monitor._report_change_tone('AuraInterruptFlags_1', '0', '16704'),
            ('mechanic', '数值调整', ''),
        )

    def test_html_report_repairs_utf8_mojibake_names(self):
        self.database_metadata.return_value = {473909: {'icon': 'inv_misc_book_09'}}
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor.name_locale = 'zhCN'
        mojibake_name = '知识宝典'.encode('utf-8').decode('latin1')
        mojibake_spec = '元素'.encode('utf-8').decode('latin1')
        monitor._fetch_spell_names_concurrent = lambda build, spell_ids, locale_override=None: {473909: mojibake_name}
        monitor._ensure_spell_names_zh = lambda branch, build, spell_ids: {473909: mojibake_name}
        monitor._load_chr_classes = lambda build, locale_override=None: {11: '德鲁伊'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {262: {'name': mojibake_spec, 'class_id': 11}}
        monitor._render_spell_primary_description = lambda *args, **kwargs: ''
        monitor._render_spell_text_plain = lambda build, spell_id, text: (str(text or ''), [])
        monitor._filter_diff_fields = lambda table_key, fields: fields

        class _EmptyValues:
            def exclude(self, **kwargs):
                return self
            def values(self, *args):
                return []
        class _EmptySnapshotManager:
            def filter(self, **kwargs):
                return _EmptyValues()

        spell_changes = {
            473909: {
                'diffs': {
                    'spellname': [
                        {'id': 473909, 'action': 'changed', 'fields': [{'field': 'Name_lang', 'before': mojibake_name, 'after': mojibake_name}]},
                    ]
                }
            }
        }

        with override_settings(BASE_DIR=str(self.base_dir)):
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects', _EmptySnapshotManager()):
                meta = monitor._write_html_report(
                    branch='wowt',
                    server_title='PTR(测试服)',
                    from_build='12.1.0.68301',
                    to_build='12.1.0.68412',
                    display_from_build='',
                    display_to_build='',
                    class_names={11: 'Druid'},
                    spec_meta={262: {'name': 'Elemental', 'class_id': 11}},
                    spell_to_specs={473909: {262}},
                    spec_to_class={262: 11},
                    spell_changes=spell_changes,
                    data_build='12.1.0.68412',
                )

        html = (self.base_dir / 'static' / meta['path']).read_text(encoding='utf-8')
        self.assertIn('知识宝典', html)
        self.assertIn('元素', html)
        self.assertIn('data-search=', html)
        self.assertIn('inv_misc_book_09.jpg', html)
        self.assertIn('https://www.wowhead.com/ptr/spell=473909', html)
        self.assertNotIn('çŸ¥è¯†', html)
        self.assertNotIn('å…ƒç´', html)
    def test_html_report_keeps_db2_keys_with_chinese_labels(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor.name_locale = 'zhCN'
        monitor._fetch_spell_names_concurrent = lambda build, spell_ids, locale_override=None: {12345: 'Scaling Spell'}
        monitor._ensure_spell_names_zh = lambda branch, build, spell_ids: {12345: '缩放技能'}
        monitor._load_chr_classes = lambda build, locale_override=None: {1: '战士'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {0: {'name': '通用', 'class_id': 1}}
        monitor._render_spell_primary_description = lambda *args, **kwargs: ''
        monitor._render_spell_text_plain = lambda build, spell_id, text: (str(text or ''), [])
        monitor._filter_diff_fields = lambda table_key, fields: fields

        class _EmptyValues:
            def exclude(self, **kwargs):
                return self
            def values(self, *args):
                return []
        class _EmptySnapshotManager:
            def filter(self, **kwargs):
                return _EmptyValues()

        spell_changes = {
            12345: {
                'diffs': {
                    'spellscaling': [
                        {'id': 12345, 'action': 'changed', 'fields': [{'field': 'MaxScalingLevel', 'before': '70', 'after': '80'}]},
                    ],
                    'traitdefinition': [
                        {'id': 987, 'action': 'changed', 'fields': [{'field': 'TraitDefinitionID', 'before': '987', 'after': '988'}]},
                    ],
                    'spelleffect': [
                        {'id': 1353090, 'action': 'changed', 'meta': {'EffectIndex': 9}, 'fields': [
                            {'field': 'EffectAura', 'before': '219', 'after': '648'},
                            {'field': 'EffectMiscValue_0', 'before': '3', 'after': '5'},
                        ]},
                        {'id': 1353103, 'action': 'changed', 'meta': {'EffectIndex': 10}, 'fields': [
                            {'field': 'EffectAura', 'before': '219', 'after': '648'},
                            {'field': 'EffectMiscValue_0', 'before': '3', 'after': '5'},
                        ]},
                        {'id': 1353104, 'action': 'changed', 'meta': {'EffectIndex': 11}, 'fields': [
                            {'field': 'EffectAura', 'before': '219', 'after': '648'},
                            {'field': 'EffectMiscValue_0', 'before': '12', 'after': '6'},
                        ]},
                        {'id': 1353105, 'action': 'changed', 'meta': {'EffectIndex': 12}, 'fields': [
                            {'field': 'EffectAura', 'before': '219', 'after': '648'},
                            {'field': 'EffectMiscValue_0', 'before': '12', 'after': '6'},
                        ]},
                    ],
                }
            }
        }

        with override_settings(BASE_DIR=str(self.base_dir)):
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects', _EmptySnapshotManager()):
                meta = monitor._write_html_report(
                    branch='wowt',
                    server_title='PTR(测试服)',
                    from_build='12.1.0.68301',
                    to_build='12.1.0.68412',
                    display_from_build='',
                    display_to_build='',
                    class_names={1: 'Warrior'},
                    spec_meta={0: {'name': 'General', 'class_id': 1}},
                    spell_to_specs={12345: {0}},
                    spec_to_class={0: 1},
                    spell_changes=spell_changes,
                    data_build='12.1.0.68412',
                    effect_record_ids=True,
                )

        html = (self.base_dir / 'static' / meta['path']).read_text(encoding='utf-8')
        self.assertIn('技能缩放 / spellscaling', html)
        self.assertIn('最高缩放等级 / MaxScalingLevel', html)
        self.assertIn('天赋定义 / traitdefinition', html)
        self.assertIn('天赋定义 ID / TraitDefinitionID', html)
        self.assertIn('SpellEffect.ID 1353090', html)
        self.assertIn('EffectAura', html)
        self.assertIn('EffectMiscValue_0', html)
        self.assertIn("class='del'>219</span> → <span class='ins'>648", html)
        self.assertIn("class='del'>3</span> → <span class='ins'>5", html)
        self.assertIn("class='del'>12</span> → <span class='ins'>6", html)
        for record_id in (1353090, 1353103, 1353104, 1353105):
            self.assertIn(f'SpellEffect.ID {record_id}', html)
        from botend.services.wow_skill_report_metadata import report_spell_entries
        self.assertEqual(report_spell_entries(html)[12345]['indices'], {9, 10, 11, 12})
        from bs4 import BeautifulSoup
        report = BeautifulSoup(html, 'html.parser')
        self.assertIn('改动来源', report.select_one('.summary').get_text())
        self.assertEqual(report.select_one('.affected-metric strong').get_text(strip=True), '—')
        self.assertEqual(report.select_one('.impact-block-title').get_text(strip=True), '本次字段与数值变化')
        facts = report.select('.impact-block [data-effect-index]')
        self.assertEqual([row['data-effect-index'] for row in facts], ['9', '10', '11', '12'])
        self.assertIn('EffectAura', facts[0].get_text())
        self.assertIn('219 → 648', facts[0].get_text(' ', strip=True))
        self.assertIn('EffectMiscValue_0', facts[2].get_text())
        self.assertIn('12 → 6', facts[2].get_text(' ', strip=True))
        self.assertIn('最高缩放等级 / MaxScalingLevel', report.select_one('.impact-block').get_text())
        assessment = report.select_one('.impact-assessment')
        self.assertIsNotNone(assessment)
        self.assertIn('影响评估', assessment.get_text())
        self.assertIn('强弱待评估', assessment.get_text())
        self.assertNotIn('需实战验证', report.select_one('.impact-block').get_text())
        self.assertIn('影响评估', report.select_one('.impact-overview-title').get_text())
        self.assertIn('查看 DB2 字段细节', html)
        self.assertIn("data-tone='mechanic'", html)

        spell_changes[12345]['diffs']['spelleffect'] = [
            {'id': record_id, 'action': 'changed', 'meta': {'EffectIndex': index, 'Effect': 6},
             'fields': [{'field': 'EffectBonusCoefficient', 'before': '1', 'after': '2'}]}
            for record_id, index in ((9001, 9), (9002, 10))
        ]
        with override_settings(BASE_DIR=str(self.base_dir)):
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects', _EmptySnapshotManager()):
                numeric_meta = monitor._write_html_report(
                    branch='wowt', server_title='PTR(测试服)',
                    from_build='12.1.0.68301', to_build='12.1.0.68412',
                    display_from_build='', display_to_build='',
                    class_names={1: 'Warrior'}, spec_meta={0: {'name': 'General', 'class_id': 1}},
                    spell_to_specs={12345: {0}}, spec_to_class={0: 1},
                    spell_changes=spell_changes, data_build='12.1.0.68412', effect_record_ids=True,
                )
        numeric = BeautifulSoup((self.base_dir / 'static' / numeric_meta['path']).read_text(encoding='utf-8'), 'html.parser')
        numeric_facts = numeric.select('.impact-block [data-effect-index]')
        self.assertEqual([row['data-effect-index'] for row in numeric_facts], ['9', '10'])
        for row, record_id in zip(numeric_facts, (9001, 9002)):
            text = row.get_text(' ', strip=True)
            self.assertIn(f'SpellEffect.ID {record_id}', text)
            self.assertIn('EffectBonusCoefficient', text)
            self.assertIn('1 → 2', text)
        self.assertNotIn('+100%', numeric.select_one('.impact-block').get_text())
        self.assertNotIn('增强', numeric.select_one('.impact-block').get_text())

        spell_changes[12345]['diffs']['spelleffect'] = [{
            'id': 0, 'action': 'changed', 'meta': {'EffectIndex': 0},
            'fields': [{'field': 'EffectAura', 'before': '219', 'after': '648'}],
        }]
        with override_settings(BASE_DIR=str(self.base_dir)):
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects', _EmptySnapshotManager()):
                hotfix_meta = monitor._write_html_report(
                    branch='wowt', server_title='PTR(测试服)',
                    from_build='12.1.0.68301', to_build='12.1.0.68412',
                    display_from_build='', display_to_build='',
                    class_names={1: 'Warrior'}, spec_meta={0: {'name': 'General', 'class_id': 1}},
                    spell_to_specs={12345: {0}}, spec_to_class={0: 1},
                    spell_changes=spell_changes, data_build='12.1.0.68412',
                )
        hotfix_html = (self.base_dir / 'static' / hotfix_meta['path']).read_text(encoding='utf-8')
        self.assertIn('EffectIndex 0', hotfix_html)
        self.assertNotIn('SpellEffect.ID ?', hotfix_html)
    def test_html_report_resolves_or_hides_unresolved_tooltip_placeholders(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor.name_locale = 'zhCN'
        monitor._fetch_spell_names_concurrent = lambda build, spell_ids, locale_override=None: {1299000: 'Placeholder Spell'}
        monitor._ensure_spell_names_zh = lambda branch, build, spell_ids: {1299000: '占位技能'}
        monitor._load_chr_classes = lambda build, locale_override=None: {1: '战士'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {0: {'name': '通用', 'class_id': 1}}
        monitor._fetch_spelleffect_rows_by_spell = lambda build, spell_id: []
        monitor._get_spelleffect_row_by_index = lambda build, spell_id, effect_index: {}
        monitor._fetch_spellmisc_by_spellid = lambda build, spell_id: {}
        monitor._filter_diff_fields = lambda table_key, fields: fields

        class _EmptyValues:
            def exclude(self, **kwargs):
                return self
            def values(self, *args):
                return []
        class _EmptySnapshotManager:
            def filter(self, **kwargs):
                return _EmptyValues()

        spell_changes = {
            1299000: {
                'diffs': {
                    'spelldescription': [
                        {
                            'id': 1299000,
                            'action': 'changed',
                            'fields': [
                                {'field': 'Description_lang', 'before': 'Damage increased by $1299405s1%.', 'after': 'Damage increased by $1299405s1%.'}
                            ],
                        },
                    ],
                }
            }
        }

        with override_settings(BASE_DIR=str(self.base_dir)):
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects', _EmptySnapshotManager()):
                meta = monitor._write_html_report(
                    branch='wowt',
                    server_title='PTR(测试服)',
                    from_build='12.1.0.68301',
                    to_build='12.1.0.68412',
                    display_from_build='',
                    display_to_build='',
                    class_names={1: 'Warrior'},
                    spec_meta={0: {'name': 'General', 'class_id': 1}},
                    spell_to_specs={1299000: {0}},
                    spec_to_class={0: 1},
                    spell_changes=spell_changes,
                    data_build='12.1.0.68412',
                )

        html = (self.base_dir / 'static' / meta['path']).read_text(encoding='utf-8')
        self.assertIn('Damage increased by x%.', html)
        self.assertNotIn('$1299405s1%', html)


class WagoHotfixFullHtmlReportTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)

    def test_class_report_writes_values_and_effect_marker_for_metadata(self):
        from botend.services.wow_skill_report_metadata import report_spell_entries
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._load_chr_classes = lambda build, locale_override=None: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {
            70: {'name': 'Retribution', 'class_id': 2},
        }
        monitor._load_specialization_spells = lambda build: {427453: {70}}
        monitor._ensure_spell_names_zh = Mock(side_effect=AssertionError('out-of-interval names queried'))
        monitor._fetch_spell_names_concurrent = Mock(side_effect=AssertionError('out-of-interval names queried'))
        monitor._render_spell_primary_description = lambda *args, **kwargs: ''
        fact = {'source': {'id': 27226894621, 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185, 'build': 69933},
                'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0',
                          'BonusCoefficientFromAP': '10.451600074768', 'PvpMultiplier': '0.5440000295639'},
                'before_verified': True, 'after_verified': True, 'changes': [
                    {'field': 'BonusCoefficientFromAP', 'before': '6.9677400588989', 'after': '10.451600074768'},
                    {'field': 'PvpMultiplier', 'before': '0.68000000715256', 'after': '0.5440000295639'},
                ]}
        name_fact = {'source': {'id': 27226894622, 'table_name': 'SpellName', 'record_id': 427453,
                                'push_id': 112185, 'build': 69933},
                     'after': {'ID': '427453', 'Name_lang': 'Hammer of Light'},
                     'before_verified': False, 'after_verified': True, 'changes': []}
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects.filter') as snapshot, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.database_spell_metadata', return_value={}):
            snapshot.return_value.values.return_value = [{'spell_id': 427453, 'name': 'Old Name', 'name_zh': '旧名'}]
            report = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112185, region_id=1, facts=[fact, name_fact], locale='enUS',
            )
            staged = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112186, region_id=1, facts=[fact, name_fact], locale='enUS',
                stage_for_publication=True,
            )
        body = (self.base_dir / 'static' / report['content_html_path']).read_text(encoding='utf-8')
        self.assertEqual(report['spell_count'], 1)
        self.assertEqual(report['class_count'], 1)
        self.assertIn('6.9677400588989', body)
        self.assertIn('10.451600074768', body)
        self.assertIn('0.5440000295639', body)
        self.assertIn('Hammer of Light', body)
        self.assertNotIn('旧名', body)
        monitor._ensure_spell_names_zh.assert_not_called()
        monitor._fetch_spell_names_concurrent.assert_not_called()
        self.assertEqual(report_spell_entries(body)[427453]['indices'], {0})
        self.assertTrue(Path(staged['staging_path']).is_file())
        self.assertFalse((self.base_dir / 'static' / staged['content_html_path']).exists())
        self.assertEqual(report_spell_entries(Path(staged['staging_path']).read_text(encoding='utf-8'))[427453]['indices'], {0})

    def test_hotfix_source_link_uses_working_locale_push_search(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        url = monitor._hotfix_url(push_id=112185, locale='enUS')
        self.assertIn('search=enUS+112185', url)
        self.assertNotIn('filter%5Bpush_id%5D', url)

    def test_hotfix_full_html_stages_outside_public_static_directory(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 1, 'region_id': 3, 'locale': 'enUS', 'table_name': 'Spell',
                  'record_id': 123, 'push_id': 112185, 'build': 69933, 'status': 1}
        fact = {'source': source, 'after': {'ID': '123', 'OtherField': 'value-not-in-summary'}, 'after_verified': True,
                'before_verified': False, 'changes': []}
        monitor._fetch_db2_row_by_id = lambda *args: {}
        with override_settings(BASE_DIR=str(self.base_dir)):
            physical, relative = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, to_push=112185,
                summary_title='热修', wago_url='https://wago.tools/hotfixes?search=enUS+112185',
                build_num='69933', db2_build='12.1.0.69933', from_push=112181,
                table_stats=[('Spell', 1)], by_table={'Spell': [source]},
                sample_per_table=1, enrich_max=0, facts=[fact], stage_for_publication=True,
            )
        self.assertTrue(Path(physical).is_file())
        self.assertIn('value-not-in-summary', Path(physical).read_text(encoding='utf-8'))
        self.assertIn('wago-hotfix-staging', physical)
        self.assertFalse((self.base_dir / 'static' / relative).exists())

    def test_full_report_explains_null_payload_invalidation_without_inventing_values(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 2, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellScriptText',
                  'record_id': 26959, 'push_id': 112194, 'build': 69875, 'status': 3, 'data': None}
        fact = {'source': source, 'after': None, 'before': None, 'after_verified': False,
                'before_verified': False, 'changes': []}
        monitor._fetch_db2_row_by_id = lambda *args: {}
        with override_settings(BASE_DIR=str(self.base_dir)):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, to_push=112208,
                summary_title='Hotfix interval', wago_url='https://wago.tools/hotfixes?search=enUS+112208',
                build_num='69875 / 69933', db2_build='12.1.0.69933', from_push=112181,
                table_stats=[('SpellScriptText', 1)], by_table={'SpellScriptText': [source]},
                sample_per_table=1, enrich_max=0, facts=[fact],
            )
        body = Path(path).read_text(encoding='utf-8')
        self.assertIn('失效', body)
        self.assertIn('职业归属未核实', body)
        self.assertIn('build 69875', body)
        self.assertNotIn('当前记录：', body)

    def test_unattributed_spell_invalidation_is_marked_partial_not_silently_lost(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._load_chr_classes = lambda build: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build: {70: {'name': 'Retribution', 'class_id': 2}}
        monitor._load_specialization_spells = lambda build: {427453: {70}}
        known = {'source': {'id': 1, 'table_name': 'SpellEffect', 'record_id': 1106904,
                            'push_id': 112185, 'build': 69933},
                 'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0',
                           'BonusCoefficientFromAP': '10.45'},
                 'after_verified': True, 'before_verified': True,
                 'changes': [{'field': 'BonusCoefficientFromAP', 'before': '6.97', 'after': '10.45'}]}
        invalidated = {'source': {'id': 2, 'table_name': 'SpellScriptText', 'record_id': 26959,
                                  'push_id': 112194, 'build': 69875, 'status': 3, 'data': None},
                       'after': None, 'after_verified': False, 'before_verified': False, 'changes': []}
        with patch.object(monitor, '_write_html_report', return_value={'path': 'portal/reports/class.html', 'class_count': 1}) as writer:
            result = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112208, region_id=3, facts=[known, invalidated],
            )
        self.assertEqual(result['spell_count'], 1)
        self.assertEqual(result['unresolved_count'], 1)
        self.assertIn('1 条', writer.call_args.kwargs['source_uncertainty_note'])

    def test_unresolved_spell_payload_cannot_publish_empty_class_projection(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._load_chr_classes = lambda build: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build: {70: {'name': 'Retribution', 'class_id': 2}}
        monitor._load_specialization_spells = lambda build: {427453: {70}}
        fact = {'source': {'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185, 'build': 69933},
                'after': None, 'after_verified': False, 'before_verified': False, 'changes': []}
        with self.assertRaisesRegex(WagoDiffUnavailable, 'unresolved Spell'):
            monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112185, region_id=1, facts=[fact], locale='enUS',
            )

    def test_class_projection_uses_separate_push_identity_and_build_style(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._load_chr_classes = lambda build: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build: {70: {'name': 'Retribution', 'class_id': 2}}
        monitor._load_specialization_spells = lambda build: {427453: {70}}
        monitor._spell_has_class = lambda sid, *args: sid == 427453
        fact = {'source': {'id': 9, 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185, 'build': 69933},
                'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0', 'BonusCoefficientFromAP': '10.451600074768'},
                'after_verified': True, 'before_verified': True, 'changes': [
                    {'field': 'BonusCoefficientFromAP', 'before': '6.9677400589', 'after': '10.451600074768'},
                ]}
        with patch.object(monitor, '_write_html_report', return_value={'path': 'portal/reports/wow_skill_diff_wow_enUS_hotfix_r1_p112185.html', 'class_count': 1}) as writer:
            result = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112185, region_id=1, facts=[fact], locale='enUS',
            )
        self.assertEqual(result['spell_count'], 1)
        self.assertEqual(result['class_count'], 1)
        self.assertEqual(writer.call_args.kwargs['report_key'], 'hotfix_r1_p112185')
        self.assertEqual(writer.call_args.kwargs['data_build'], '12.1.0.69933')
        self.assertTrue(writer.call_args.kwargs['effect_record_ids'])
        self.assertEqual(writer.call_args.kwargs['spell_changes'][427453]['diffs']['spelleffect'][0]['id'], 1106904)

    def test_full_report_reads_hotfix_payload_values_not_base_row(self):
        from bs4 import BeautifulSoup
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 9, 'push_id': 112185, 'region_id': 1, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 1106904, 'status': 1}
        before = {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0',
                  'BonusCoefficientFromAP': '6.9677400589', 'PvpMultiplier': '0.68000000715'}
        after = {**before, 'BonusCoefficientFromAP': '10.451600074768', 'PvpMultiplier': '0.5440000295639'}
        facts = [{'source': source, 'before': before, 'after': after, 'before_verified': True,
                  'after_verified': True, 'changes': [
                      {'field': 'BonusCoefficientFromAP', 'before': before['BonusCoefficientFromAP'], 'after': after['BonusCoefficientFromAP']},
                      {'field': 'PvpMultiplier', 'before': before['PvpMultiplier'], 'after': after['PvpMultiplier']},
                  ]}]
        def base_row(table, build, rid):
            return {'ID': rid, 'Name_lang': 'Hammer of Light'} if table.lower() == 'spellname' else before
        monitor._fetch_db2_row_by_id = base_row
        monitor._fetch_spell_names_concurrent = lambda build, ids, locale_override=None: {427453: 'Hammer of Light'}
        with override_settings(BASE_DIR=str(self.base_dir)):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=1, to_push=112185,
                summary_title='Hotfix push 112185', wago_url='https://wago.tools/hotfixes?search=enUS+112185',
                build_num='69933', db2_build='12.1.0.69933', from_push=112181,
                table_stats=[('SpellEffect', 1)], by_table={'SpellEffect': [source]},
                sample_per_table=20, enrich_max=20, facts=facts,
            )
        soup = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        card = soup.select_one('.reader-card')
        self.assertIn('Hammer of Light', card.get_text(' ', strip=True))
        self.assertIn('BonusCoefficientFromAP', card.get_text(' ', strip=True))
        self.assertIn('6.9677400589', card.get_text(' ', strip=True))
        self.assertIn('10.451600074768', card.get_text(' ', strip=True))
        self.assertIn('0.5440000295639', card.get_text(' ', strip=True))
        self.assertNotIn('当前记录：攻击强度系数 6.9677400589', card.get_text(' ', strip=True))

    def test_hotfix_fallback_html_uses_the_same_impact_report_hierarchy(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'zhCN'

        with override_settings(BASE_DIR=str(self.base_dir)):
            report = monitor._build_hotfix_fallback_report(
                branch='wow',
                current_build='68367',
                from_push=109505,
                to_push=109506,
                locale='zhCN',
                reason='明细接口暂时不可用。',
                source_report={'table_count': 3, 'entry_count': 8},
            )

        html = (self.base_dir / 'static' / report['content_html_path']).read_text(encoding='utf-8')
        self.assertIn('hotfix-report-fallback', html)
        self.assertIn('Wago Hotfix 影响报告 · 数据待补全', html)
        self.assertIn('影响范围暂时无法可靠还原', html)
        self.assertIn('不会猜测受影响对象，也不会给出增强或削弱结论', html)
        self.assertIn('当前已知 DB2 表', html)
        self.assertIn('当前已知记录', html)

    def test_hotfix_full_html_enriches_records_instead_of_record_id_list(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'

        def fake_fetch(table, build, record_id):
            key = str(table).lower()
            if key == 'spellname':
                return {'ID': record_id, 'Name_lang': 'Arcane Surge', 'VerifiedBuild': build}
            if key == 'spelleffect':
                return {
                    'ID': record_id,
                    'SpellID': 365350,
                    'EffectIndex': 0,
                    'Effect': 6,
                    'EffectAura': 13,
                    'EffectBasePointsF': '15',
                    'EffectBonusCoefficient': '0.42',
                    'PvpMultiplier': '0.8',
                    'Flags': 0,
                    'CameraEnteringDelay': 0,
                    'VerifiedBuild': build,
                }
            if key == 'spellscaling':
                return {
                    'ID': record_id,
                    'SpellID': 365350,
                    'MaxScalingLevel': 80,
                    'ScalesFromItemLevel': 1,
                    'VerifiedBuild': build,
                }
            if key == 'questv2':
                return {
                    'ID': record_id,
                    'Name_lang': 'Repair the Beacon',
                    'QuestID': record_id,
                    'QuestSortID': 42,
                    'Flags': 1024,
                    'VerifiedBuild': build,
                }
            return {'ID': record_id, 'Name_lang': f'{table} readable row {record_id}', 'VerifiedBuild': build}

        monitor._fetch_db2_row_by_id = fake_fetch
        def fake_extract_spell_id(table_key, row):
            return int((row or {}).get('SpellID') or 0)

        def fake_fetch_spell_names(build, spell_ids, locale_override=None):
            return {int(i): 'Arcane Surge' for i in spell_ids}

        monitor._extract_spell_id = fake_extract_spell_id
        monitor._fetch_spell_names_concurrent = fake_fetch_spell_names

        with override_settings(BASE_DIR=str(self.base_dir)):
            full_path, rel_path = monitor._write_hotfix_full_html(
                branch='wow',
                locale='enUS',
                to_push=109505,
                summary_title='Hotfix 全量更新：2 张表 / 3 项（push 109504→109505）',
                wago_url='https://wago.tools/hotfixes?filter%5Bpush_id%5D=109505',
                build_num='62706',
                from_push=109504,
                table_stats=[('SpellEffect', 2), ('SpellScaling', 1), ('ItemSparse', 1), ('QuestV2', 1), ('Map', 1)],
                by_table={
                    'SpellEffect': [
                        {'push_id': 109505, 'table_name': 'SpellEffect', 'record_id': 777},
                    ],
                    'SpellScaling': [
                        {'push_id': 109505, 'table_name': 'SpellScaling', 'record_id': 778},
                    ],
                    'ItemSparse': [
                        {'push_id': 109505, 'table_name': 'ItemSparse', 'record_id': 19019},
                    ],
                    'QuestV2': [
                        {'push_id': 109505, 'table_name': 'QuestV2', 'record_id': 84621},
                    ],
                    'Map': [
                        {'push_id': 109505, 'table_name': 'Map', 'record_id': 2552},
                    ],
                },
                sample_per_table=5,
                enrich_max=20,
            )

        html = Path(full_path).read_text(encoding='utf-8')

        self.assertEqual(rel_path, 'portal/reports/wow_hotfix_full_wow_enUS_109505.html')
        self.assertIn('hotfixFilter', html)
        self.assertIn('技能效果 / SpellEffect', html)
        self.assertIn('Arcane Surge', html)
        self.assertIn('基础数值F / EffectBasePointsF', html)
        self.assertIn('技能缩放 / SpellScaling', html)
        self.assertIn('最高缩放等级 / MaxScalingLevel', html)
        self.assertIn('随物品等级缩放 / ScalesFromItemLevel', html)
        self.assertIn('任务 / QuestV2', html)
        self.assertIn('Repair the Beacon', html)
        self.assertIn('QuestSortID', html)
        self.assertIn('地图 / Map', html)
        self.assertIn('Wago push 109505', html)
        self.assertIn('ItemSparse readable row 19019', html)
        self.assertIn('DB2 表目录（按类别分组，覆盖全部表）', html)
        self.assertIn('筛选表名、record_id、字段值', html)
        self.assertIn('class="hotfix-report"', html)
        self.assertIn("aria-label='按影响范围筛选'", html)
        self.assertIn("data-hotfix-category='all'", html)
        self.assertIn("data-hotfix-category='技能/法术'", html)
        self.assertIn('不能作为热修生效值或变化幅度', html)
        self.assertIn('这次具体改了什么', html)
        self.assertIn('第 1 个技能效果的客户端 DB2 基表记录（非热修生效值）：基础值 15，法术强度系数 0.42，PvP 倍率 0.8', html)
        self.assertIn('当前设置为随物品等级缩放', html)
        self.assertIn('查看技术明细与 DB2 基表参考字段', html)
        self.assertIn('先看对象和字段', html)
        self.assertIn('字段关系', html)
        self.assertIn('DB2 基表参考字段（最多前 120 个', html)
        self.assertIn('class=\'fields important-fields\'', html)
        self.assertNotIn("<div class='field primary'><span>标志位</span><strong>0</strong></div>", html)
        self.assertNotIn("<div class='field primary'><span>进入相机延迟</span><strong>0</strong></div>", html)
        self.assertIn('查看 2 条 DB2 记录', html)
        self.assertNotIn('<summary><b>SpellEffect</b>（2）</summary><ul><li><code>777</code></li>', html)

    def test_hotfix_full_html_cleans_unresolved_spell_tooltip_placeholders(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'

        def fake_fetch(table, build, record_id):
            if str(table).lower() == 'spelldescription':
                return {
                    'ID': record_id,
                    'SpellID': 1299000,
                    'Description_lang': 'Damage increased by $1299405s1%.',
                    'VerifiedBuild': build,
                }
            return {}

        monitor._fetch_db2_row_by_id = fake_fetch
        monitor._fetch_spelleffect_rows_by_spell = lambda build, spell_id: []
        monitor._get_spelleffect_row_by_index = lambda build, spell_id, effect_index: {}
        monitor._fetch_spellmisc_by_spellid = lambda build, spell_id: {}
        monitor._fetch_spell_names_concurrent = lambda build, spell_ids, locale_override=None: {1299000: 'Placeholder Spell'}

        with override_settings(BASE_DIR=str(self.base_dir)):
            full_path, _rel_path = monitor._write_hotfix_full_html(
                branch='wow',
                locale='enUS',
                to_push=109506,
                summary_title='Hotfix 全量更新：tooltip 占位符测试',
                wago_url='https://wago.tools/hotfixes?filter%5Bpush_id%5D=109506',
                build_num='68367',
                from_push=109505,
                table_stats=[('SpellDescription', 1)],
                by_table={'SpellDescription': [{'push_id': 109506, 'table_name': 'SpellDescription', 'record_id': 1299000}]},
                sample_per_table=5,
                enrich_max=20,
            )

        html = Path(full_path).read_text(encoding='utf-8')
        self.assertIn('Damage increased by x%.', html)
        self.assertNotIn('$1299405s1%', html)

    def test_hotfix_full_report_uses_current_build_for_db2_enrichment(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'

        pages = {
            1: [
                {
                    'push_id': 109522,
                    'locale': 'enUS',
                    'table_name': 'SpellEffect',
                    'record_id': 267,
                    'build': '68275',
                },
            ],
            2: [],
        }

        def fake_fetch_page(build_num='', page=1, *, search=''):
            return pages.get(page, [])

        def fake_write_html(**kwargs):
            self.assertEqual(kwargs['build_num'], '68275')
            self.assertEqual(kwargs['db2_build'], '12.0.7.68367')
            return str(self.base_dir / 'static' / 'portal' / 'reports' / 'hotfix.html'), 'portal/reports/hotfix.html'

        def fake_fetch_db2_row(table, build, record_id):
            self.assertEqual(build, '12.0.7.68367')
            return {'ID': record_id, 'SpellID': 686, 'Name_lang': 'Shadow Bolt'}

        monitor._fetch_hotfix_page_data = fake_fetch_page
        monitor._write_hotfix_full_html = fake_write_html
        monitor._fetch_db2_row_by_id = fake_fetch_db2_row

        with override_settings(
            BASE_DIR=str(self.base_dir),
            WAGO_HOTFIX_MAX_PAGES=2,
            WAGO_HOTFIX_REPORT_ENRICH_MAX=5,
        ):
            result = monitor._generate_hotfix_full_report('wow', '12.0.7.68367', 109506, 109522, locale='enUS')

        self.assertIsNotNone(result)
        self.assertEqual(result['build_num'], '68275')
        self.assertEqual(result['build_str'], '12.0.7.68367')

    def test_hotfix_full_html_includes_object_graph_view(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'zhCN'

        rows = {
            ('SpellEffect', 777): {
                'ID': 777,
                'SpellID': 365350,
                'EffectIndex': 0,
                'BonusCoefficientFromAP': '1.25',
            },
            ('SpellName', 365350): {'ID': 365350, 'Name_lang': '奥术涌动'},
            ('QuestV2CliTask', 888): {
                'ID': 888,
                'QuestID': 84621,
                'ObjectiveText_lang': '点亮信标',
            },
            ('QuestV2', 84621): {'ID': 84621, 'Title_lang': '修复信标'},
            ('ItemEffect', 999): {'ID': 999, 'ParentItemID': 19019, 'SpellID': 365350},
            ('ItemSparse', 19019): {'ID': 19019, 'Display_lang': '奥术饰品'},
        }

        fetch_locales = []

        def fake_fetch(table, build, record_id):
            fetch_locales.append(monitor.locale)
            return rows.get((str(table), int(record_id))) or {}

        monitor._fetch_db2_row_by_id = fake_fetch

        with override_settings(BASE_DIR=str(self.base_dir)):
            full_path, rel_path = monitor._write_hotfix_full_html(
                branch='wow',
                locale='zhCN',
                to_push=109505,
                summary_title='Hotfix 全量更新：对象视图测试',
                wago_url='https://wago.tools/hotfixes?filter%5Bpush_id%5D=109505',
                build_num='68367',
                from_push=109504,
                table_stats=[('SpellEffect', 1), ('QuestV2CliTask', 1), ('ItemEffect', 1)],
                by_table={
                    'SpellEffect': [{'push_id': 109505, 'table_name': 'SpellEffect', 'record_id': 777}],
                    'QuestV2CliTask': [{'push_id': 109505, 'table_name': 'QuestV2CliTask', 'record_id': 888}],
                    'ItemEffect': [{'push_id': 109505, 'table_name': 'ItemEffect', 'record_id': 999}],
                },
                sample_per_table=5,
                enrich_max=20,
            )

        html = Path(full_path).read_text(encoding='utf-8')
        self.assertEqual(rel_path, 'portal/reports/wow_hotfix_full_wow_zhCN_109505.html')
        self.assertIn('这次具体改了什么', html)
        self.assertIn('奥术涌动', html)
        self.assertIn('修复信标', html)
        self.assertIn('奥术饰品', html)
        self.assertIn('关联技能', html)
        self.assertIn('奥术涌动 #365350', html)
        self.assertTrue(fetch_locales)
        self.assertEqual(set(fetch_locales), {'zhCN'})
        self.assertEqual(monitor.locale, 'zhCN')

    def test_hotfix_full_html_explains_tables_when_db2_details_missing(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'zhCN'

        def fail_fetch(table, build, record_id):
            raise AssertionError('enrich disabled should not fetch DB2 detail')

        monitor._fetch_db2_row_by_id = fail_fetch

        with override_settings(BASE_DIR=str(self.base_dir)):
            full_path, _rel_path = monitor._write_hotfix_full_html(
                branch='wow',
                locale='zhCN',
                to_push=109484,
                summary_title='Hotfix 全量更新：语义兜底测试',
                wago_url='https://wago.tools/hotfixes?filter%5Bpush_id%5D=109484',
                build_num='68367',
                from_push=109452,
                table_stats=[('VehicleSeat', 5), ('BattlePetSpecies', 2), ('ModifierTree', 1)],
                by_table={
                    'VehicleSeat': [{'push_id': 109484, 'table_name': 'VehicleSeat', 'record_id': 26184}],
                    'BattlePetSpecies': [{'push_id': 109484, 'table_name': 'BattlePetSpecies', 'record_id': 4602}],
                    'ModifierTree': [{'push_id': 109452, 'table_name': 'ModifierTree', 'record_id': 457185}],
                },
                sample_per_table=5,
                enrich_max=0,
            )

        html = Path(full_path).read_text(encoding='utf-8')
        self.assertIn('先看对象和字段', html)
        self.assertIn('载具座位 / VehicleSeat', html)
        self.assertIn('载具/交互', html)
        self.assertIn('VehicleSeat.ID = 座位记录', html)
        self.assertIn('战斗宠物品种 / BattlePetSpecies', html)
        self.assertIn('条件/规则树 / ModifierTree', html)
        self.assertIn('未读取到当前行字段', html)
        self.assertIn('VehicleSeat #26184', html)
        self.assertNotIn('可能影响载具座位、乘坐交互、动作按钮', html)
        self.assertNotIn('可读解释', html)
        self.assertNotIn('该 DB2 记录没有可展示字段', html)

    def test_hotfix_full_html_renders_mount_object_and_field_labels(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'zhCN'

        rows = {
            ('Mount', 1111): {
                'ID': 1111,
                'Name_lang': '星界水母',
                'SourceSpellID': 2222,
                'CreatureDisplayInfoID': 3333,
            },
            ('SpellName', 2222): {'ID': 2222, 'Name_lang': '召唤星界水母'},
        }

        def fake_fetch(table, build, record_id):
            return rows.get((str(table), int(record_id))) or {}

        monitor._fetch_db2_row_by_id = fake_fetch

        with override_settings(BASE_DIR=str(self.base_dir)):
            full_path, _rel_path = monitor._write_hotfix_full_html(
                branch='wow',
                locale='zhCN',
                to_push=109506,
                summary_title='Hotfix 全量更新：坐骑对象测试',
                wago_url='https://wago.tools/hotfixes?filter%5Bpush_id%5D=109506',
                build_num='68367',
                from_push=109505,
                table_stats=[('Mount', 1)],
                by_table={'Mount': [{'push_id': 109506, 'table_name': 'Mount', 'record_id': 1111}]},
                sample_per_table=5,
                enrich_max=20,
            )

        html = Path(full_path).read_text(encoding='utf-8')
        self.assertIn('这次具体改了什么', html)
        self.assertIn('<h3>星界水母</h3>', html)
        self.assertIn('来源技能', html)
        self.assertIn('召唤星界水母 #2222', html)
        self.assertIn('生物外观 ID', html)
        self.assertIn('Mount.ID = MountID', html)
        self.assertNotIn('可读解释', html)
        self.assertIn('查看技术明细与 DB2 基表参考字段', html)
        self.assertIn('坐骑获取与展示', html)
        self.assertIn('不能作为热修生效值或变化幅度', html)

    def test_hotfix_full_report_scans_bounded_pages_even_when_pushes_are_not_monotonic(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'

        pages = {
            1: [
                {'push_id': 109431, 'locale': 'enUS', 'table_name': 'Phase', 'record_id': 1, 'build': '68367'},
                {'push_id': 109502, 'locale': 'esMX', 'table_name': 'SpellScriptText', 'record_id': 2, 'build': '68367'},
            ],
            2: [
                {'push_id': 109502, 'locale': 'enUS', 'table_name': 'ItemCurrencyCost', 'record_id': 3, 'build': '68367'},
            ],
            3: [],
        }

        def fake_fetch_page(build_num='', page=1, *, search=''):
            return pages.get(page, [])

        def fake_write_html(**kwargs):
            self.assertEqual(kwargs['table_stats'], [('ItemCurrencyCost', 1)])
            self.assertEqual(kwargs['by_table']['ItemCurrencyCost'][0]['record_id'], 3)
            return str(self.base_dir / 'static' / 'portal' / 'reports' / 'hotfix.html'), 'portal/reports/hotfix.html'

        def fail_fetch_db2_row(table, build, record_id):
            raise AssertionError('WAGO_HOTFIX_REPORT_ENRICH_MAX=0 should disable DB2 row enrichment')

        monitor._fetch_hotfix_page_data = fake_fetch_page
        monitor._fetch_db2_row_by_id = fail_fetch_db2_row
        monitor._write_hotfix_full_html = fake_write_html

        with override_settings(
            BASE_DIR=str(self.base_dir),
            WAGO_HOTFIX_MAX_PAGES=3,
            WAGO_HOTFIX_REPORT_ENRICH_MAX=0,
        ):
            result = monitor._generate_hotfix_full_report('wow', '68367', 109431, 109502, locale='enUS')

        self.assertIsNotNone(result)
        self.assertEqual(result['entry_count'], 1)
        self.assertEqual(result['table_count'], 1)
        self.assertEqual(result['changed_tables_json'], '["ItemCurrencyCost"]')


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalReportFileViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)
        report_dir = self.base_dir / 'static' / 'portal' / 'reports'
        report_dir.mkdir(parents=True)
        (report_dir / 'ok.html').write_text('<h1>ok report</h1>', encoding='utf-8')
        nested_dir = report_dir / 'nested'
        nested_dir.mkdir()
        (nested_dir / 'ok.html').write_text('<h1>nested report</h1>', encoding='utf-8')
        (self.base_dir / 'static' / 'secret.html').write_text('secret', encoding='utf-8')

    def test_legacy_hotfix_route_warns_that_base_values_are_not_hotfix_facts(self):
        path = self.base_dir / 'static' / 'portal' / 'reports' / 'legacy.html'
        path.write_text('<html><body><h1>旧 Hotfix 报告</h1></body></html>', encoding='utf-8')
        row = SimpleNamespace(id=123, content_html_path='portal/reports/legacy.html', collection_complete=False)
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(row)):
            response = PortalWowHotfixReportView.as_view()(
                self.factory.get('/portal/wow-hotfix-report/123/'), report_id=123,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('历史数值未核实', response.content.decode('utf-8'))
        self.assertIn('旧 Hotfix 报告', response.content.decode('utf-8'))
        row.collection_complete = True
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(row)):
            verified = PortalWowHotfixReportView.as_view()(
                self.factory.get('/portal/wow-hotfix-report/123/'), report_id=123,
            )
        self.assertNotIn('历史数值未核实', verified.content.decode('utf-8'))

    def test_standalone_legacy_hotfix_html_also_warns(self):
        name = 'wow_hotfix_full_wow_enUS_112208.html'
        (self.base_dir / 'static' / 'portal' / 'reports' / name).write_text(
            '<html><body><h1>旧 Hotfix 报告</h1></body></html>', encoding='utf-8',
        )
        row = SimpleNamespace(id=123, content_html_path='portal/reports/' + name, collection_complete=False)
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(row)):
            response = PortalReportFileView.as_view()(
                self.factory.get('/portal/reports/' + name), report_path=name,
            )
        self.assertIn('历史数值未核实', response.content.decode('utf-8'))

    def test_report_url_maps_content_html_path_to_portal_endpoint(self):
        self.assertEqual(
            portal_report_url('portal/reports/wow_hotfix_full_wow_zhCN_109505.html'),
            '/portal/reports/wow_hotfix_full_wow_zhCN_109505.html',
        )
        self.assertEqual(
            portal_report_url('/static/portal/reports/wow_hotfix_full_wow_zhCN_109505.html'),
            '/portal/reports/wow_hotfix_full_wow_zhCN_109505.html',
        )
        self.assertEqual(
            _normalize_portal_url('/static/portal/reports/wow_hotfix_full_wow_zhCN_109505.html'),
            '/portal/reports/wow_hotfix_full_wow_zhCN_109505.html',
        )

    def test_report_file_view_serves_only_allowed_html_reports(self):
        with override_settings(BASE_DIR=str(self.base_dir)):
            request = self.factory.get('/portal/reports/ok.html')
            response = PortalReportFileView.as_view()(request, report_path='ok.html')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertIn('ok report', response.content.decode('utf-8'))

    def test_report_file_view_allows_nested_report_paths(self):
        with override_settings(BASE_DIR=str(self.base_dir)):
            request = self.factory.get('/portal/reports/nested/ok.html')
            response = PortalReportFileView.as_view()(request, report_path='nested/ok.html')

        self.assertEqual(response.status_code, 200)
        self.assertIn('nested report', response.content.decode('utf-8'))

    def test_standalone_skill_report_with_icons_still_loads_metadata(self):
        name = 'wow_skill_diff_wow_enUS_12_1_0_69875.html'
        (self.base_dir / 'static' / 'portal' / 'reports' / name).write_text(
            "<html><body><article class='spell' id='spell-123'></article></body></html>", encoding='utf-8',
        )
        row = SimpleNamespace(id=27, branch='wow')
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowSkillDiffReport.objects.filter') as lookup:
            lookup.return_value.first.return_value = row
            response = PortalReportFileView.as_view()(
                self.factory.get(f'/portal/reports/{name}'), report_path=name,
            )
        body = response.content.decode('utf-8')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/portal/api/wow-skill-diff/27/metadata/', body)
        self.assertIn('wow-skill-report-metadata.js', body)

    def test_uncommitted_hotfix_class_standalone_file_is_not_public(self):
        name = 'wow_skill_diff_wow_enUS_hotfix_r3_p112208.html'
        (self.base_dir / 'static' / 'portal' / 'reports' / name).write_text(
            '<html><body><h1>尚未提交的职业报告</h1></body></html>', encoding='utf-8',
        )
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(None)):
            response = PortalReportFileView.as_view()(
                self.factory.get('/portal/reports/' + name), report_path=name,
            )
        self.assertEqual(response.status_code, 404)

    def test_standalone_hotfix_class_report_uses_hotfix_metadata_not_build_id(self):
        name = 'wow_skill_diff_wow_enUS_hotfix_r1_p112185.html'
        (self.base_dir / 'static' / 'portal' / 'reports' / name).write_text(
            "<html><body><article class='spell' id='spell-123'></article></body></html>", encoding='utf-8',
        )
        row = SimpleNamespace(id=81, branch='wow', collection_complete=True, class_spell_count=1)
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects.filter') as lookup:
            lookup.return_value.first.return_value = row
            response = PortalReportFileView.as_view()(
                self.factory.get(f'/portal/reports/{name}'), report_path=name,
            )
        body = response.content.decode('utf-8')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/portal/api/wow-hotfix-class/81/metadata/', body)
        self.assertNotIn('/portal/api/wow-skill-diff/81/metadata/', body)

    def test_report_file_view_rejects_path_traversal(self):
        blocked_paths = [
            '../secret.html',
            'nested/../../secret.html',
            '/etc/passwd',
            'nested\\..\\secret.html',
            'ok.txt',
        ]
        with override_settings(BASE_DIR=str(self.base_dir)):
            for report_path in blocked_paths:
                self.assertIsNone(_resolve_portal_report_html_path(report_path), report_path)
                request = self.factory.get(f'/portal/reports/{report_path}')
                response = PortalReportFileView.as_view()(request, report_path=report_path)
                self.assertEqual(response.status_code, 404, report_path)

@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalWowHotfixClassReportViewTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)
        report_dir = self.base_dir / 'static' / 'portal' / 'reports'
        report_dir.mkdir(parents=True)
        self.content = '<html><body><section class="spell" id="spell-123">效果(#0)</section></body></html>'
        (report_dir / 'wow_skill_diff_hotfix_r1_p112185.html').write_text(self.content, encoding='utf-8')
        self.row = SimpleNamespace(
            id=81, branch='wow', region_id=1, from_push=112181, to_push=112185,
            build_str='12.1.0.69933', created_at=None, collection_complete=True,
            class_spell_count=1, class_class_count=1,
            class_unresolved_count=8,
            source_facts_json=json.dumps([{'source_build': '12.1.0.69814',
                                           'source': {'table_name': 'SpellEffect', 'record_id': 456},
                                           'after_verified': True,
                                           'after': {'SpellID': '123', 'EffectIndex': '0'}}]),
            class_content_html_path='portal/reports/wow_skill_diff_hotfix_r1_p112185.html',
        )

    def test_hotfix_class_detail_and_metadata_use_frozen_source_build_and_typed_route(self):
        with override_settings(BASE_DIR=str(self.base_dir)), \
             patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(self.row)), \
             patch('botend.portal.views.build_hotfix_report_spell_metadata', return_value={123: {'id': 123}}) as build:
            response = PortalWowHotfixClassReportView.as_view()(
                RequestFactory().get('/portal/wow-hotfix-class/81/'), report_id=81,
            )
            metadata = PortalWowHotfixClassMetadataAPIView.as_view()(
                RequestFactory().get('/portal/api/wow-hotfix-class/81/metadata/'), report_id=81,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-skill-report-metadata="/portal/api/wow-hotfix-class/81/metadata/"', response.content.decode())
        self.assertIn('push 112181', response.content.decode())
        self.assertEqual(json.loads(metadata.content)['spells']['123']['id'], 123)
        self.assertEqual(json.loads(metadata.content)['unresolved_count'], 8)
        self.assertEqual(json.loads(metadata.content)['relation_unresolved_count'], 0)
        build.assert_called_once_with(self.content, 'wow', json.loads(self.row.source_facts_json),
                                      resolve_remote=False)

    def test_unverified_class_projection_is_not_public(self):
        self.row.collection_complete = False
        with patch('botend.portal.views.WowHotfixReport.objects', _FakeManager(self.row)):
            response = PortalWowHotfixClassReportView.as_view()(
                RequestFactory().get('/portal/wow-hotfix-class/81/'), report_id=81,
            )
        self.assertEqual(response.status_code, 404)

class WagoSkillDiffMonitorCursorTests(SimpleTestCase):
    def test_hotfix_complete_flag_save_failure_removes_published_files(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        with tempfile.TemporaryDirectory() as folder:
            public = Path(folder) / 'published.html'
            public.write_text('staged result', encoding='utf-8')
            state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=112181,
                                    save=lambda update_fields: None)
            source = {'id': 1, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                      'table_name': 'SpellEffect', 'record_id': 1106904}
            fact = {'source': source, 'after_verified': True, 'after': {'SpellID': '427453'}}
            full = {'entry_count': 1, 'content_html_path': 'portal/reports/full.html',
                    'staging_path': '/tmp/private-full.html', 'collection_complete': True}
            cls = {'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
                   'content_html_path': 'portal/reports/class.html', 'staging_path': '/tmp/private-class.html'}
            saved = SimpleNamespace(collection_complete=False,
                                    save=Mock(side_effect=[OSError('commit failed'), None]))
            with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185), \
                 patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
                 patch.object(monitor, '_resolve_hotfix_facts', return_value=[fact]), \
                 patch.object(monitor, '_generate_hotfix_full_report', return_value=full), \
                 patch.object(monitor, '_generate_hotfix_class_report', return_value=cls), \
                 patch.object(monitor, '_publish_staged_hotfix_reports', return_value=[str(public)]), \
                 patch.object(monitor, '_mark_event'), \
                 patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', return_value=(SimpleNamespace(), True)), \
                 patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as existing, \
                 patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.update_or_create', return_value=(saved, True)):
                existing.return_value.first.return_value = None
                self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
            self.assertFalse(public.exists())
            self.assertFalse(saved.collection_complete)
            self.assertEqual(state.hotfix_push_id, 112181)

    def test_successful_retry_preserves_already_complete_hotfix_report(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=112181,
                                save=lambda update_fields: None)
        source = {'id': 1, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 1106904}
        fact = {'source': source, 'after_verified': True, 'after': {'SpellID': '427453'}}
        full = {'entry_count': 1, 'content_html_path': 'portal/reports/full.html',
                'staging_path': '/tmp/private-full.html', 'collection_complete': True}
        cls = {'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
               'content_html_path': 'portal/reports/class.html', 'staging_path': '/tmp/private-class.html'}
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185), \
             patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
             patch.object(monitor, '_resolve_hotfix_facts', return_value=[fact]), \
             patch.object(monitor, '_generate_hotfix_full_report', return_value=full), \
             patch.object(monitor, '_generate_hotfix_class_report', return_value=cls), \
             patch.object(monitor, '_discard_hotfix_staging') as discard, \
             patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', return_value=(SimpleNamespace(), True)), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as existing, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.update_or_create') as overwrite:
            existing.return_value.first.return_value = SimpleNamespace(collection_complete=True)
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        overwrite.assert_not_called()
        discard.assert_called_once_with(full, cls)
        self.assertEqual(state.hotfix_push_id, 112181)

    def test_hotfix_publication_failure_marks_row_incomplete_and_keeps_cursor(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=112181,
                                save=lambda update_fields: None)
        source = {'id': 1, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 1106904}
        fact = {'source': source, 'after_verified': True, 'after': {'SpellID': '427453'}}
        full = {'entry_count': 1, 'content_html_path': 'portal/reports/full.html',
                'staging_path': '/tmp/private-full.html', 'collection_complete': True}
        cls = {'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
               'content_html_path': 'portal/reports/class.html', 'staging_path': '/tmp/private-class.html'}
        row = SimpleNamespace(collection_complete=True, save=lambda update_fields: None)
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185), \
             patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
             patch.object(monitor, '_resolve_hotfix_facts', return_value=[fact]), \
             patch.object(monitor, '_generate_hotfix_full_report', return_value=full), \
             patch.object(monitor, '_generate_hotfix_class_report', return_value=cls), \
             patch.object(monitor, '_publish_staged_hotfix_reports', side_effect=OSError('disk full')), \
             patch.object(monitor, '_mark_event') as event, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', return_value=(SimpleNamespace(), True)), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as existing, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.update_or_create', return_value=(row, True)):
            existing.return_value.first.return_value = None
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        self.assertEqual(state.hotfix_push_id, 112181)
        self.assertFalse(row.collection_complete)
        self.assertEqual(state.hotfix_last_event_status, 'publish_failed')
        self.assertEqual(event.call_args.kwargs['status'], 'publish_failed')

    def test_staged_hotfix_publication_is_bounded_and_rolls_back_partial_move(self):
        import os
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            def reports(push):
                a, b = monitor._hotfix_report_private_path(), monitor._hotfix_report_private_path()
                Path(a).write_text('全量事实', encoding='utf-8')
                Path(b).write_text('职业事实', encoding='utf-8')
                return ({'content_html_path': f'portal/reports/wow_hotfix_full_wow_enUS_r3_{push}.html',
                         'staging_path': a},
                        {'content_html_path': f'portal/reports/wow_skill_diff_wow_enUS_hotfix_r3_p{push}.html',
                         'staging_path': b})
            full, cls = reports(112185)
            monitor._publish_staged_hotfix_reports(full, cls)
            for item in (full, cls):
                self.assertFalse(Path(item['staging_path']).exists())
                self.assertTrue((Path(folder) / 'static' / item['content_html_path']).is_file())
            full, cls = reports(112208)
            original = os.replace
            def fail_second(source, dest):
                if source == cls['staging_path']:
                    raise OSError('second move failed')
                return original(source, dest)
            with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.os.replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'second move failed'):
                    monitor._publish_staged_hotfix_reports(full, cls)
            for item in (full, cls):
                self.assertFalse((Path(folder) / 'static' / item['content_html_path']).exists())
            monitor._discard_hotfix_staging(full, cls)
            self.assertFalse(Path(cls['staging_path']).exists())

    def test_unknown_legacy_region_never_reuses_unscoped_cursor(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=0, hotfix_push_id=112208, save=lambda update_fields: None)
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185) as fetch:
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        fetch.assert_not_called()
        self.assertEqual(state.hotfix_push_id, 112208)
        self.assertEqual(state.hotfix_last_event_status, 'hotfix_region_unverified')

    def test_failed_hotfix_retry_does_not_overwrite_previously_complete_report(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        class State:
            hotfix_push_id = 112181
            hotfix_region_id = 3
            def save(self, update_fields):
                pass
        state = State()
        existing = SimpleNamespace(id=55, collection_complete=True)
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185), \
             patch.object(monitor, '_collect_hotfix_interval_rows', side_effect=WagoDiffUnavailable('source incomplete')), \
             patch.object(monitor, '_build_hotfix_fallback_report', return_value={
                 'content_html_path': 'portal/reports/incomplete.html', 'entry_count': 0,
             }), patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', return_value=(SimpleNamespace(), True)), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects') as reports:
            reports.filter.return_value.first.return_value = existing
            self.assertFalse(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        reports.update_or_create.assert_not_called()
        self.assertEqual(state.hotfix_push_id, 112181)

    def test_hotfix_scan_publishes_two_views_from_one_complete_region_fact_set(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

        class State:
            hotfix_push_id = 112181
            hotfix_region_id = 1
            def save(self, update_fields):
                pass

        state = State()
        source = {'id': 9, 'push_id': 112185, 'region_id': 1, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 1106904, 'data': [1106904]}
        fact = {'source': source, 'after': {'ID': '1106904', 'SpellID': '427453'},
                'before': None, 'changes': [], 'before_verified': False}
        full = {'to_push': 112185, 'from_push': 112181, 'build_str': '12.1.0.69933',
                'summary_title': 'Hotfix 全量更新', 'content_html_path': 'portal/reports/all.html',
                'report_url': '/portal/reports/all.html', 'entry_count': 1, 'table_count': 1,
                'changed_tables_json': '["SpellEffect"]', 'source_facts_json': json.dumps([fact]),
                'collection_complete': True, 'staging_path': '/tmp/staged-all.html'}
        cls = {'spell_count': 1, 'class_count': 1,
               'content_html_path': 'portal/reports/class.html', 'unresolved_count': 0,
               'staging_path': '/tmp/staged-class.html'}
        saved_row = SimpleNamespace(id=55, collection_complete=False, save=Mock())
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=112185) as latest, \
             patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]) as collect, \
             patch.object(monitor, '_resolve_hotfix_facts', return_value=[fact]) as resolve, \
             patch.object(monitor, '_generate_hotfix_full_report', return_value=full) as full_writer, \
             patch.object(monitor, '_generate_hotfix_class_report', return_value=cls) as class_writer, \
             patch.object(monitor, '_publish_staged_hotfix_reports') as publish, \
             patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', return_value=(SimpleNamespace(), True)) as events, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as existing, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.update_or_create', return_value=(saved_row, True)) as reports:
            existing.return_value.first.return_value = None
            self.assertTrue(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        latest.assert_called_once_with(locale='enUS', region_id=1, current_build='12.1.0.69933')
        collect.assert_called_once_with(112181, 112185, region_id=1, locale='enUS')
        resolve.assert_called_once_with([source], db2_build='12.1.0.69933', interval_only=False)
        self.assertIs(full_writer.call_args.kwargs['facts'], class_writer.call_args.kwargs['facts'])
        self.assertTrue(full_writer.call_args.kwargs['stage_for_publication'])
        self.assertTrue(class_writer.call_args.kwargs['stage_for_publication'])
        publish.assert_called_once_with(full, cls)
        self.assertEqual(events.call_args.kwargs['region_id'], 1)
        self.assertEqual(reports.call_args.kwargs['region_id'], 1)
        self.assertEqual(reports.call_args.kwargs['defaults']['class_content_html_path'], cls['content_html_path'])
        self.assertEqual(reports.call_args.kwargs['defaults']['class_unresolved_count'], 0)
        self.assertEqual(reports.call_args.kwargs['defaults']['source_facts_json'], full['source_facts_json'])
        self.assertFalse(reports.call_args.kwargs['defaults']['collection_complete'])
        self.assertTrue(saved_row.collection_complete)
        saved_row.save.assert_called_once_with(update_fields=['collection_complete'])
        self.assertEqual(state.hotfix_push_id, 112185)

    def test_hotfix_interval_reuses_exact_push_rows_and_rejects_excess(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        expected = [
            {'id': 1, 'push_id': 112183, 'record_id': 891602},
            {'id': 2, 'push_id': 112185, 'record_id': 1106904},
        ]
        def fetch(push_id, **kwargs):
            self.assertEqual(kwargs, {'region_id': 1, 'locale': 'enUS'})
            return [r for r in expected if r['push_id'] == push_id]
        with patch.object(monitor, '_fetch_hotfix_push_rows', side_effect=fetch) as lookup:
            self.assertEqual(monitor._collect_hotfix_interval_rows(112181, 112185, region_id=1, locale='enUS'), expected)
        self.assertEqual(lookup.call_count, 4)
        with override_settings(WAGO_HOTFIX_MAX_ENTRIES=1), patch.object(monitor, '_fetch_hotfix_push_rows', side_effect=fetch):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'Hotfix.*limit'):
                monitor._collect_hotfix_interval_rows(112181, 112185, region_id=1, locale='enUS')

    def test_hotfix_discovery_uses_exact_region_and_checks_more_than_first_page(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        pages = {
            1: [{'push_id': 111863, 'region_id': 3, 'locale': 'enUS'},
                {'push_id': 112208, 'region_id': 1, 'locale': 'enUS'}],
            2: [{'push_id': 112185, 'region_id': 3, 'locale': 'enUS'}],
            3: [],
        }
        with override_settings(WAGO_HOTFIX_MAX_PAGES=3), \
             patch.object(monitor, '_fetch_hotfix_page_data', side_effect=lambda page, search: pages[page]):
            self.assertEqual(monitor._fetch_latest_hotfix_push_id(locale='enUS', region_id=3), 112185)

    def test_hotfix_push_collection_verifies_pages_and_region_before_reporting(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

        def inertia(page, rows, *, total=3, last_page=2):
            payload = {'props': {'filters': {'search': 'enUS 112185'}, 'hotfixes': {
                'total': total, 'current_page': page, 'last_page': last_page,
                'per_page': 2, 'data': rows,
            }}}
            return '<div data-page="' + html.escape(json.dumps(payload), quote=True) + '"></div>'

        us = {'id': 101, 'push_id': 112185, 'region_id': 1, 'locale': 'enUS',
              'table_name': 'SpellEffect', 'record_id': 1106904, 'status': 1,
              'data': [1106904, '10.451600074768']}
        eu = {**us, 'id': 102, 'region_id': 3}
        another = {**us, 'id': 103, 'push_id': 112181}

        def page_for(url, **kwargs):
            self.assertIn('search=enUS+112185', url)
            return inertia(2, [another]) if 'page=2' in url else inertia(1, [us, eu])

        with patch.object(monitor, '_http_get_text', side_effect=page_for):
            rows = monitor._fetch_hotfix_push_rows(112185, region_id=1, locale='enUS')
        self.assertEqual(rows, [us])
        with patch.object(monitor, '_http_get_text', side_effect=lambda url, **kwargs: inertia(1, [us, eu])):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'Hotfix.*incomplete'):
                monitor._fetch_hotfix_push_rows(112185, region_id=1, locale='enUS')
        with patch.object(monitor, '_http_get_text', side_effect=lambda url, **kwargs: inertia(1, [us, us], total=2, last_page=1)):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'Hotfix.*duplicate'):
                monitor._fetch_hotfix_push_rows(112185, region_id=1, locale='enUS')

    def test_late_seen_older_hotfix_push_never_resets_cursor_or_replaces_report(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

        class State:
            hotfix_push_id = 112208
            hotfix_region_id = 3
            hotfix_last_event_status = 'has_update'
            def save(self, update_fields):
                pass

        state = State()
        with patch.object(monitor, '_fetch_latest_hotfix_push_id', return_value=111863), \
             patch.object(monitor, '_fetch_prev_hotfix_push_id', side_effect=AssertionError('no predecessor lookup')), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create', side_effect=AssertionError('no event write')), \
             patch.object(monitor, '_generate_hotfix_full_report', side_effect=AssertionError('no replay')):
            self.assertTrue(monitor._scan_hotfix_if_needed(state, 'wow', '12.1.0.69933'))
        self.assertEqual(state.hotfix_push_id, 112208)
        self.assertEqual(state.hotfix_last_event_status, 'hotfix_source_behind_cursor')

    @override_settings(WAGO_SKILL_DIFF_MAX_DIFF_ROWS=3)
    def test_oversized_diff_compares_complete_exact_build_csv_instead_of_first_page(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

        def inertia(payload):
            return '<div data-page="' + html.escape(json.dumps({'props': payload}), quote=True) + '"></div>'

        def fetch_page(url, **kwargs):
            if '/diff?' in url:
                return inertia({'entries': {
                    'total': 4, 'current_page': 1, 'last_page': 2, 'per_page': 3,
                    'data': [
                        {'ID': '1', 'Description_lang': 'unchanged', 'Action': 'changed', 'oldData': {'ID': '1', 'Description_lang': 'unchanged'}},
                        {'ID': '2', 'Description_lang': 'new', 'Action': 'changed', 'oldData': {'ID': '2', 'Description_lang': 'old'}},
                        {'ID': '4', 'Description_lang': 'added', 'Action': 'added'},
                    ],
                    'next_page_url': 'https://wago.tools/db2/Spell/diff?page=2',
                }})
            if '/db2/Spell?build=' in url:
                build = url.split('build=', 1)[1].split('&', 1)[0]
                return inertia({'filters': {'build': build, 'locale': monitor.locale}, 'data': {'total': 3}})
            raise AssertionError(url)

        old = b'ID,Description_lang\n1,"unchanged\nsecond line"\n2,old\n3,removed\n'
        new = b'ID,Description_lang\n1,"unchanged\nsecond line"\n2,new\n4,added\n'

        class AutoClosingRaw(BytesIO):
            def _close_at_end(self, result):
                if result and self.tell() == len(self.getvalue()):
                    self.close()
                return result

            def read(self, size=-1):
                return self._close_at_end(super().read(size))

            def read1(self, size=-1):
                return self._close_at_end(super().read1(size))

        class CsvResponse:
            status_code = 200

            def __init__(self, data, build):
                self.raw = AutoClosingRaw(data)
                self.data = data
                self.headers = {'Content-Type': 'text/csv', 'Content-Disposition': f'attachment; filename="Spell.{build}.csv"'}

            def iter_lines(self, chunk_size=None, delimiter=None):
                return iter(self.data.split(delimiter or b'\n'))

            def iter_content(self, chunk_size=None):
                for start in range(0, len(self.data), 7):
                    yield self.data[start:start + 7]

            def close(self):
                self.raw.close()

        def fetch_csv(url, **kwargs):
            self.assertTrue(kwargs.get('stream'), url)
            return CsvResponse(old if 'build=old' in url else new, 'old' if 'build=old' in url else 'new')

        with patch.object(monitor, '_http_get_text', side_effect=fetch_page), patch.object(monitor._http_session, 'get', side_effect=fetch_csv):
            rows = monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

        self.assertEqual({int(row['ID']): row['Action'] for row in rows}, {2: 'changed', 3: 'removed', 4: 'added'})
        self.assertEqual(next(row for row in rows if row['ID'] == '2')['oldData']['Description_lang'], 'old')

        def truncated_csv(url, **kwargs):
            return CsvResponse(old if 'build=old' in url else b'ID,Description_lang\n1,unchanged\n2,new\n', 'old' if 'build=old' in url else 'new')

        with patch.object(monitor, '_http_get_text', side_effect=fetch_page), patch.object(monitor._http_session, 'get', side_effect=truncated_csv):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'incomplete'):
                monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

        def unidentified_csv(url, **kwargs):
            response = fetch_csv(url, **kwargs)
            response.headers.pop('Content-Disposition')
            return response

        with patch.object(monitor, '_http_get_text', side_effect=fetch_page), patch.object(monitor._http_session, 'get', side_effect=unidentified_csv):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'identity'):
                monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

        with override_settings(WAGO_SKILL_DIFF_MAX_TEMP_BYTES=1):
            with patch.object(monitor, '_http_get_text', side_effect=fetch_page), patch.object(monitor._http_session, 'get', side_effect=fetch_csv):
                with self.assertRaisesRegex(WagoDiffUnavailable, 'temporary storage'):
                    monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

        cr_only = b'ID,Description_lang\r1,"first\rsecond"\r2,last\r'
        with patch.object(monitor._http_session, 'get', return_value=CsvResponse(cr_only, 'old')):
            cr_rows = list(monitor._iter_db2_csv_rows('Spell', 'old'))
        self.assertEqual(len(cr_rows), 2)
        self.assertEqual(cr_rows[0]['Description_lang'], 'first\rsecond')

        crlf = b'ID,Description_lang\r\n1,"first\r\nsecond"\r\n2,last\r\n'
        with patch.object(monitor._http_session, 'get', return_value=CsvResponse(crlf, 'old')):
            crlf_rows = list(monitor._iter_db2_csv_rows('Spell', 'old'))
        self.assertEqual(len(crlf_rows), 2)
        self.assertEqual(crlf_rows[0]['Description_lang'], 'first\r\nsecond')

    def test_paginated_diff_incomplete_total_is_not_returned_as_no_change(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        payload = html.escape(json.dumps({'props': {'entries': {
            'total': 2, 'current_page': 1, 'data': [{'ID': '1', 'Action': 'changed'}], 'next_page_url': None,
        }}}), quote=True)
        with patch.object(monitor, '_http_get_text', return_value=f'<div data-page="{payload}"></div>'):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'incomplete'):
                monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

    def test_paginated_diff_duplicate_id_cannot_pass_total_check(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        def fetch_page(url):
            page = 2 if 'page=2' in url else 1
            payload = html.escape(json.dumps({'props': {'entries': {
                'total': 2, 'current_page': page, 'data': [{'ID': '1', 'Action': 'changed'}],
                'next_page_url': 'https://wago.tools/db2/Spell/diff?page=2' if page == 1 else None,
            }}}), quote=True)
            return f'<div data-page="{payload}"></div>'
        with patch.object(monitor, '_http_get_text', side_effect=fetch_page):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'duplicate'):
                monitor._fetch_db2_diff_rows('Spell', 'old', 'new')

    def test_build_manifest_incomplete_does_not_skip_a_core_table(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        payload = html.escape(json.dumps({'props': {'items': {
            'total': 2, 'current_page': 1, 'data': [
                {'Type': 'db2', 'Filename': 'dbfilesclient/spell.db2', 'Action': 'modified', 'FDID': 1},
            ], 'next_page_url': None,
        }}}), quote=True)
        with patch.object(monitor, '_http_get_text', return_value=f'<div data-page="{payload}"></div>'):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'incomplete'):
                monitor._fetch_changed_db2_tables('old', 'new')

    def test_missing_class_mapping_does_not_advance_as_no_class_change(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._fetch_changed_db2_tables = lambda from_build, to_build: {'Spell'}
        monitor._load_chr_classes = lambda build: {}
        monitor._load_chr_specialization_meta = lambda build: {}
        monitor._load_specialization_spells = lambda build: {}
        with self.assertRaisesRegex(WagoDiffUnavailable, 'class attribution'):
            monitor._generate_report('wow', 'old', 'new')

    def test_diff_unavailable_report_is_explicit_not_empty_no_change(self):
        from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoDiffUnavailable

        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor._fetch_changed_db2_tables = lambda from_build, to_build: (_ for _ in ()).throw(
            WagoDiffUnavailable('Wago builds-diff is not available yet')
        )

        report = monitor._generate_report('wowt', '12.1.0.68301', '12.1.0.68412')

        self.assertTrue(report.get('diff_unavailable'))
        self.assertEqual(report.get('spell_count'), 0)
        self.assertIn('not available', report.get('error', ''))

    def test_manual_rerun_diff_unavailable_does_not_create_empty_report(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor._generate_report = lambda branch, from_build, to_build, *args, **kwargs: {
            'diff_unavailable': True,
            'error': 'Wago builds-diff is not available yet',
            'spell_count': 0,
            'class_count': 0,
            'changed_tables_json': '[]',
        }

        result = monitor.rerun_build_diff('wowt', '12.1.0.68301', '12.1.0.68412', 'enUS')

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('status'), 'diff_unavailable')
        self.assertNotIn('report_id', result)
        self.assertIn('not available', result.get('error', ''))

    def test_scan_state_processes_pending_and_records_next_interval_without_skipping_build(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.default_branch = 'wowt'
        monitor._fetch_current_build = lambda branch: '12.1.0.68500'
        monitor._latest_discovered_build = lambda st: '12.1.0.68412'
        calls = []

        def fake_process_pending(st, limit=1):
            calls.append(('process_pending', st.build, limit))
            return False

        def fake_record(st, from_build, to_build, is_init=False):
            calls.append(('record_event', from_build, to_build, is_init))
            return SimpleNamespace(id=1)

        monitor._process_pending_build_events = fake_process_pending
        monitor._record_build_event = fake_record
        state = SimpleNamespace(
            branch='wowt',
            build='12.1.0.68301',
            save=lambda *args, **kwargs: None,
        )

        result = monitor._scan_state(state)

        self.assertFalse(result)
        self.assertEqual(calls, [
            ('process_pending', '12.1.0.68301', 1),
            ('record_event', '12.1.0.68412', '12.1.0.68500', False),
        ])
    def test_build_scoped_hotfix_candidate_is_not_claimed_as_complete_search(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        def payload(page):
            rows = (
                [{'id': 91, 'region_id': 1, 'locale': 'enUS', 'build': 69933, 'push_id': 112300},
                 {'id': 92, 'region_id': 3, 'locale': 'enUS', 'build': 69933, 'push_id': 112218}]
                if page == 1 else
                [{'id': 93, 'region_id': 3, 'locale': 'enUS', 'build': 69933, 'push_id': 112215}]
            )
            return {'hotfixes': {'current_page': page, 'last_page': 163, 'total': 4075,
                                 'per_page': 25, 'data': rows}}
        with override_settings(WAGO_HOTFIX_DISCOVERY_MAX_PAGES=2), \
             patch.object(monitor, '_http_get_text', side_effect=lambda url, **kw: payload(int(url.rsplit('page=', 1)[1]))), \
             patch.object(monitor, '_extract_inertia_props', side_effect=lambda value: value):
            self.assertEqual(monitor._fetch_latest_hotfix_push_id(
                locale='enUS', region_id=3, current_build='12.1.0.69933',
            ), 112218)
    def test_hotfix_facts_use_each_source_builds_schema_not_current_build_for_all(self):
        from collections import OrderedDict
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._build_versions_cache = {'versions': ['12.1.0.69875', '12.1.0.69933']}
        old = {'id': 1, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect',
               'record_id': 100, 'push_id': 112206, 'build': 69875, 'data': [100, 427453]}
        new = {'id': 2, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect',
               'record_id': 101, 'push_id': 112208, 'build': 69933, 'data': [101, 427453, '0.544']}
        def fetch(table, build, rid):
            fields = [('ID', rid), ('SpellID', 427453)]
            if build == '12.1.0.69933':
                fields.append(('PvpMultiplier', '1'))
            return OrderedDict(fields)
        with patch.object(monitor, '_fetch_db2_row_by_id', side_effect=fetch) as lookup, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.collect_hotfix_record_history', return_value=[]):
            facts = monitor._resolve_hotfix_facts([old, new], db2_build='12.1.0.69933')
        self.assertTrue(all(fact['after_verified'] for fact in facts))
        self.assertEqual(facts[0]['after']['SpellID'], '427453')
        self.assertEqual(facts[1]['after']['PvpMultiplier'], '0.544')
        self.assertEqual([fact['source_build'] for fact in facts], ['12.1.0.69875', '12.1.0.69933'])
        self.assertEqual([call.args[1] for call in lookup.call_args_list], ['12.1.0.69875', '12.1.0.69933'])
    def test_old_build_class_spell_keeps_its_historical_specialization(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._build_versions_cache = {'versions': ['12.1.0.69875', '12.1.0.69933']}
        monitor._load_chr_classes = lambda build: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build: {70: {'name': 'Retribution', 'class_id': 2}}
        monitor._load_specialization_spells = lambda build: ({427453: {70}} if build.endswith('69875') else {123: {70}})
        monitor._spell_has_class = lambda sid, _mapping, _classes, build: sid == 427453 and build.endswith('69875')
        fact = {'source': {'id': 5, 'table_name': 'SpellEffect', 'record_id': 1106904,
                           'push_id': 112206, 'build': 69875},
                'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0', 'PvpMultiplier': '0.68'},
                'before_verified': True, 'after_verified': True,
                'changes': [{'field': 'PvpMultiplier', 'before': '0.8', 'after': '0.68'}]}
        with patch.object(monitor, '_write_html_report', return_value={'path': 'portal/reports/class.html', 'class_count': 1}) as writer:
            result = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 112181, 112208, region_id=3, facts=[fact], locale='enUS',
            )
        self.assertEqual(result['spell_count'], 1)
        self.assertEqual(writer.call_args.kwargs['spell_to_specs'][427453], {70})
        self.assertEqual(fact['source_build'], '12.1.0.69875')
        self.assertEqual(writer.call_args.kwargs['spell_changes'][427453]['diffs']['spelleffect'][0]['meta']['SourceBuild'], '12.1.0.69875')
        self.assertEqual(writer.call_args.kwargs['source_build_label'], '12.1.0.69875')
        self.assertEqual(writer.call_args.kwargs['display_from_build'], 'push 112181')
    def test_interval_report_lists_all_source_builds_not_only_majority_build(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        rows = [
            {'id': 1, 'region_id': 3, 'locale': 'enUS', 'push_id': 112206,
             'table_name': 'SpellEffect', 'record_id': 101, 'build': 69875},
            {'id': 2, 'region_id': 3, 'locale': 'enUS', 'push_id': 112208,
             'table_name': 'SpellEffect', 'record_id': 102, 'build': 69933},
        ]
        facts = [{'source': row, 'after': {'ID': str(row['record_id']), 'SpellID': '427453'},
                  'after_verified': True, 'before_verified': False, 'changes': []} for row in rows]
        with override_settings(WAGO_HOTFIX_REPORT_ENRICH_MAX=0), \
             patch.object(monitor, '_write_hotfix_full_html', return_value=('/tmp/report.html', 'portal/reports/report.html')) as writer:
            result = monitor._generate_hotfix_full_report(
                'wow', '12.1.0.69933', 112181, 112208, locale='enUS', region_id=3,
                hotfix_rows=rows, facts=facts,
            )
        self.assertIn('69875', result['content_md'])
        self.assertIn('69933', result['content_md'])
        self.assertEqual(result['build_num'], '69875 / 69933')
        self.assertEqual(writer.call_args.kwargs['build_num'], '69875 / 69933')
    def test_added_hotfix_record_decodes_from_same_build_table_schema_when_base_id_missing(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 3, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellTargetRestrictions',
                  'record_id': 346822, 'push_id': 112207, 'build': 69875, 'status': 1,
                  'data': [346822, 0, '30', 0, 0, 0, 0, '0', 1264379]}
        sample = {'ID': 1, 'DifficultyID': 0, 'ConeDegrees': 0, 'MaxTargets': 0,
                  'MaxTargetLevel': 0, 'TargetCreatureType': 0, 'Targets': 0, 'Width': 0, 'SpellID': 1}
        props = {'filters': {'build': '12.1.0.69875', 'locale': 'enUS'},
                 'data': {'data': [sample]}}
        with patch.object(monitor, '_fetch_db2_row_by_id', return_value={}), \
             patch.object(monitor, '_http_get_text', return_value='table-response') as fetch, \
             patch.object(monitor, '_extract_inertia_props', return_value=props), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.collect_hotfix_record_history', return_value=[]):
            fact = monitor._resolve_hotfix_facts([source], db2_build='12.1.0.69875')[0]
        self.assertTrue(fact['after_verified'])
        self.assertEqual(fact['after']['SpellID'], '1264379')
        self.assertIn('build=12.1.0.69875', fetch.call_args.args[0])
    def test_manual_hotfix_interval_uses_given_bounds_without_discovery(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=111863,
                                save=lambda **kwargs: None)
        class StopAtCollection(BaseException):
            pass
        with patch.object(monitor, '_fetch_latest_hotfix_push_id') as discover, \
             patch.object(monitor, '_collect_hotfix_interval_rows', side_effect=StopAtCollection) as collect, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create',
                   return_value=(SimpleNamespace(), True)):
            with self.assertRaises(StopAtCollection):
                monitor._scan_hotfix_if_needed(
                    state, 'wow', '12.1.0.69933', backfill_interval=(111863, 112236))
        discover.assert_not_called()
        collect.assert_called_once_with(111863, 112236, region_id=3, locale='enUS')

    def test_manual_backfill_source_failure_does_not_publish_fallback(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=111863,
                                save=lambda **kwargs: None)
        with patch.object(monitor, '_collect_hotfix_interval_rows', side_effect=WagoDiffUnavailable('missing page')), \
             patch.object(monitor, '_build_hotfix_fallback_report') as fallback, \
             patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as reports, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create',
                   return_value=(SimpleNamespace(), True)):
            reports.return_value.first.return_value = None
            self.assertFalse(monitor._scan_hotfix_if_needed(
                state, 'wow', '12.1.0.69933', backfill_interval=(111863, 112236)))
        fallback.assert_not_called()

    def test_manual_backfill_success_cannot_advance_real_state_cursor(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=111863,
                                save=lambda **kwargs: None)
        # Even a caller that passes a persisted state must not let a manual
        # replay rewrite its cursor.
        source = {'id': 1, 'region_id': 3, 'locale': 'enUS', 'push_id': 112236,
                  'table_name': 'SpellEffect', 'record_id': 1}
        fact = {'source': source, 'after_verified': True, 'after': {'SpellID': '12345'}}
        full = {'entry_count': 1, 'content_html_path': 'portal/reports/full.html',
                'staging_path': '/tmp/full.html'}
        cls = {'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
               'content_html_path': 'portal/reports/class.html', 'staging_path': '/tmp/class.html'}
        row = SimpleNamespace(collection_complete=False, save=Mock())
        with patch.object(monitor, '_fetch_latest_hotfix_push_id') as discover, \
             patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
             patch.object(monitor, '_resolve_hotfix_facts', return_value=[fact]), \
             patch.object(monitor, '_generate_hotfix_class_report', return_value=cls), \
             patch.object(monitor, '_generate_hotfix_full_report', return_value=full), \
             patch.object(monitor, '_publish_staged_hotfix_reports', return_value=['/tmp/published.html']), \
             patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create',
                   return_value=(SimpleNamespace(), True)), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.filter') as reports, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowHotfixReport.objects.update_or_create',
                   return_value=(row, True)):
            reports.return_value.first.return_value = None
            self.assertTrue(monitor._scan_hotfix_if_needed(
                state, 'wow', '12.1.0.69933', backfill_interval=(111863, 112236)))
        discover.assert_not_called()
        self.assertEqual(state.hotfix_push_id, 111863)
    def test_not_public_spell_rows_are_unresolved_not_a_report_failure(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        facts = [
            {'source': {'id': sid, 'push_id': 111926, 'region_id': 3,
                        'locale': 'enUS', 'table_name': table, 'record_id': 1309109,
                        'build': 69587, 'status': 4, 'data': None},
             'after_verified': False, 'after': None, 'changes': []}
            for sid, table in ((26432887223, 'SpellName'), (26432887224, 'Spell'))
        ]
        with patch.object(monitor, '_load_chr_classes', return_value={2: 'Paladin'}), \
             patch.object(monitor, '_load_chr_specialization_meta', return_value={70: {'class_id': 2}}):
            result = monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 111863, 112236,
                region_id=3, facts=facts, locale='enUS', stage_for_publication=True)
        self.assertEqual(result['unresolved_count'], 2)
        self.assertEqual(result['spell_count'], 0)
    def test_manual_report_names_do_not_upsert_spell_snapshots(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._hotfix_report_only = True
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects.filter') as snapshots, \
             patch.object(monitor, '_fetch_spell_names_concurrent', return_value={427453: '光明之锤'}), \
             patch.object(monitor, '_bulk_upsert_snapshots') as upsert:
            snapshots.return_value.exclude.return_value.values.return_value = []
            names = monitor._ensure_spell_names_zh('wow', '12.1.0.69933', [427453])
        self.assertEqual(names[427453], '光明之锤')
        upsert.assert_not_called()
    def test_manual_backfill_rejects_source_id_mismatch_before_generating_report(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=111863,
                                save=lambda **kwargs: None)
        source = {'id': 1, 'push_id': 112236, 'region_id': 3, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 10}
        with patch.object(monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
             patch.object(monitor, '_resolve_hotfix_facts') as resolve, \
             patch.object(monitor, '_mark_event'), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowWagoHotfixEvent.objects.update_or_create',
                   return_value=(SimpleNamespace(), True)):
            self.assertFalse(monitor._scan_hotfix_if_needed(
                state, 'wow', '12.1.0.69933', backfill_interval=(111863, 112236),
                expected_source_count=2244, expected_source_sha256='0' * 64))
        resolve.assert_not_called()
    def test_concurrent_hotfix_scan_cannot_publish_over_same_region(self):
        import fcntl
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        state = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=111863)
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            lock = Path(folder) / 'tmp' / 'wago-hotfix-scan-r3.lock'
            lock.parent.mkdir(parents=True)
            with lock.open('a+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(monitor, '_scan_hotfix_locked') as work:
                    self.assertFalse(monitor._scan_hotfix_if_needed(
                        state, 'wow', '12.1.0.69933', backfill_interval=(111863, 112236)))
                    work.assert_not_called()
    def test_cross_build_spec_reassignment_is_not_labeled_as_both_specs(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._full_hotfix_source_build = lambda source, current: f"12.1.0.{source['build']}"
        monitor._load_chr_classes = lambda build, locale_override=None: {2: 'Paladin'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {
            70: {'name': 'Retribution', 'class_id': 2},
            71: {'name': 'Holy', 'class_id': 2},
        }
        monitor._load_specialization_spells = lambda build: {427453: {70 if build.endswith('69875') else 71}}
        monitor._spell_class_ids = lambda sid, mappings, classes, version: {2}
        facts = [
            {'source': {'id': i, 'build': build, 'push_id': push, 'region_id': 3,
                        'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': i},
             'after': {'ID': str(i), 'SpellID': '427453', 'EffectIndex': '0', 'PvpMultiplier': str(i)},
             'after_verified': True, 'before_verified': True,
             'changes': [{'field': 'PvpMultiplier', 'before': '1', 'after': str(i)}]}
            for i, build, push in ((101, 69875, 112185), (102, 69933, 112208))
        ]
        with patch.object(monitor, '_write_html_report', return_value={
                'path': 'portal/reports/class.html', 'class_count': 1}) as writer:
            monitor._generate_hotfix_class_report(
                'wow', '12.1.0.69933', 111863, 112236,
                region_id=3, facts=facts, locale='enUS')
        self.assertEqual(writer.call_args.kwargs['spell_to_specs'][427453], {-2})
    def test_interval_only_facts_use_previous_push_without_historical_requests(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor._full_hotfix_source_build = lambda source, current: '12.1.0.69933'
        monitor._fetch_db2_row_by_id = lambda table, build, rid: {'ID': rid, 'Value': '0'}
        rows = [
            {'id': sid, 'push_id': push, 'region_id': 3, 'locale': 'enUS',
             'table_name': 'SpellEffect', 'record_id': 7, 'build': 69933,
             'status': 1, 'data': [7, value]}
            for sid, push, value in [(1, 111864, '10'), (2, 112236, '20')]
        ]
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.collect_hotfix_record_history',
                   side_effect=AssertionError('out-of-interval Wago history queried')):
            facts = monitor._resolve_hotfix_facts(rows, db2_build='12.1.0.69933', interval_only=True)
        self.assertFalse(facts[0]['before_verified'])
        self.assertTrue(facts[1]['before_verified'])
        self.assertEqual(facts[1]['changes'], [{'field': 'Value', 'before': '10', 'after': '20'}])
    def test_verified_facts_use_snapshot_name_labels_without_unbounded_wago_lookups(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 5, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect',
                  'record_id': 1106904, 'push_id': 112185, 'build': 69933, 'status': 1}
        fact = {'source': source, 'after': {'ID': '1106904', 'SpellID': '427453',
                'EffectIndex': '0', 'BonusCoefficientFromAP': '10.45'},
                'after_verified': True, 'before_verified': False, 'changes': []}
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects.filter') as snapshots, \
             patch.object(monitor, '_fetch_spell_names_concurrent') as network_names:
            snapshots.return_value.values.return_value = [{'spell_id': 427453, 'name': 'Hammer of Light'}]
            monitor._fetch_db2_row_by_id = lambda *args: {}
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, to_push=112185,
                summary_title='Hotfix interval', wago_url='https://wago.tools/hotfixes?search=enUS+112185',
                build_num='69933', db2_build='12.1.0.69933', from_push=111863,
                table_stats=[('SpellEffect', 1)], by_table={'SpellEffect': [source]},
                sample_per_table=1, enrich_max=0, facts=[fact],
            )
            network_names.assert_not_called()
            self.assertIn('Hammer of Light', Path(path).read_text(encoding='utf-8'))

