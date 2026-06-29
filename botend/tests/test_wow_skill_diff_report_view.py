from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from botend.portal.views import PortalWowSkillDiffReportView
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html


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
