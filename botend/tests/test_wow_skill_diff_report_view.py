from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.portal.views import (
    PortalReportFileView,
    PortalWowSkillDiffReportView,
    _resolve_portal_report_html_path,
    portal_report_url,
)
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html
from botend.portal.api import _normalize_url as _normalize_portal_url
from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor


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

    def test_existing_html_report_uses_iframe(self):
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

        response = self._get(report)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('<iframe', html)
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


class WagoHotfixFullHtmlReportTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base_dir = Path(self.tmpdir.name)

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
                table_stats=[('SpellEffect', 2), ('ItemSparse', 1), ('QuestV2', 1), ('Map', 1)],
                by_table={
                    'SpellEffect': [
                        {'push_id': 109505, 'table_name': 'SpellEffect', 'record_id': 777},
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
        self.assertIn('基础数值F', html)
        self.assertIn('任务 / QuestV2', html)
        self.assertIn('Repair the Beacon', html)
        self.assertIn('QuestSortID', html)
        self.assertIn('地图 / Map', html)
        self.assertIn('Wago push 109505', html)
        self.assertIn('ItemSparse readable row 19019', html)
        self.assertIn('DB2 表目录（按类别分组，覆盖全部表）', html)
        self.assertIn('技能/天赋只是其中一类', html)
        self.assertIn('筛选类别、表名、record_id、名称、描述、字段值', html)
        self.assertIn('Hotfix 原始数据只给出 push / DB2 表 / record_id', html)
        self.assertNotIn('<summary><b>SpellEffect</b>（2）</summary><ul><li><code>777</code></li>', html)


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
