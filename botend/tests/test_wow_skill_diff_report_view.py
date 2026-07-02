from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import json
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

    def test_html_report_repairs_utf8_mojibake_names(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor.name_locale = 'zhCN'
        mojibake_name = '知识宝典'.encode('utf-8').decode('latin1')
        monitor._fetch_spell_names_concurrent = lambda build, spell_ids, locale_override=None: {473909: mojibake_name}
        monitor._ensure_spell_names_zh = lambda branch, build, spell_ids: {473909: mojibake_name}
        monitor._load_chr_classes = lambda build, locale_override=None: {11: '德鲁伊'}
        monitor._load_chr_specialization_meta = lambda build, locale_override=None: {0: {'name': '通用', 'class_id': 11}}
        monitor._render_spell_primary_description = lambda *args, **kwargs: ''
        monitor._render_spell_text_plain = lambda build, spell_id, text: (str(text or ''), [])
        monitor._filter_diff_fields = lambda _tkey, fields: fields

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
                    spec_meta={0: {'name': 'General', 'class_id': 11}},
                    spell_to_specs={473909: {0}},
                    spec_to_class={0: 11},
                    spell_changes=spell_changes,
                    data_build='12.1.0.68412',
                )

        html = (self.base_dir / 'static' / meta['path']).read_text(encoding='utf-8')
        self.assertIn('知识宝典', html)
        self.assertNotIn('çŸ¥è¯†å¤æ', html)


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
                    'Flags': 0,
                    'CameraEnteringDelay': 0,
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
        self.assertIn('先看对象和字段', html)
        self.assertIn('字段关系', html)
        self.assertIn('具体游戏对象', html)
        self.assertIn('查看完整 DB2 原始字段（含 0 / 默认值 / 内部字段）', html)
        self.assertIn('class=\'fields important-fields\'', html)
        self.assertNotIn("<div class='field primary'><span>标志位</span><strong>0</strong></div>", html)
        self.assertNotIn("<div class='field primary'><span>进入相机延迟</span><strong>0</strong></div>", html)
        self.assertIn('查看 2 条 DB2 记录', html)
        self.assertNotIn('<summary><b>SpellEffect</b>（2）</summary><ul><li><code>777</code></li>', html)

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
        self.assertIn('具体游戏对象', html)
        self.assertIn('技能/法术 · 奥术涌动', html)
        self.assertIn('任务 · 修复信标', html)
        self.assertIn('物品/装备 · 奥术饰品', html)
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
        self.assertIn('具体游戏对象', html)
        self.assertIn('坐骑 · 星界水母', html)
        self.assertIn('来源技能', html)
        self.assertIn('召唤星界水母 #2222', html)
        self.assertIn('生物外观 ID', html)
        self.assertIn('Mount.ID = MountID', html)
        self.assertNotIn('可读解释', html)
        self.assertNotIn('可能影响', html)

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

class WagoSkillDiffMonitorCursorTests(SimpleTestCase):
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

