"""Future daily/weekly editions consume the shared palette, never infer channels."""
from copy import deepcopy
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.wow_daily_report.generator import render_daily_html


class BluepostChannelReportColorsTests(SimpleTestCase):
    def sections(self):
        # Deliberately contradictory titles/labels: only game_version owns color.
        channels = [
            {'game_version': 'retail', 'game_version_label': '正式服'},
            {'game_version': 'forever', 'game_version_label': '无限服'},
            {'game_version': 'test', 'game_version_label': '测试服'},
            {'game_version': '', 'game_version_label': '正式服'},
            {'game_version': None, 'game_version_label': '无限服'},
            {'game_version_label': '测试服'},
            {'game_version': 'future', 'game_version_label': '测试服'},
            {'game_version': 'retail" onmouseover="alert(1)',
             'game_version_label': '<svg onload="alert(1)"> & 正式服'},
            {'source': 'wowhead', 'game_version': 'retail', 'game_version_label': '正式服'},
            {'source': 'blizzard_tracker_other', 'game_version': 'forever', 'game_version_label': '无限服'},
            {'game_version': 'test', 'game_version_label': ''},
        ]
        items = [{
            'source': 'blizzard_tracker', 'title': f'PTR Forever 正式服 <标题> {index}',
            'url': f'https://example.invalid/news/{index}?a=1&b=2',
            'portal_url': f'/portal/news/{index}/',
            'summary': '已有摘要第一行\n第二行 & <不应变成标签>',
            'summary_llm_ok': index % 2 == 0,
            'body_html': '<p>完整原文 &amp; 译文 <strong>保持不动</strong></p>',
            **channel,
        } for index, channel in enumerate(channels)]
        return [
            {'key': 'news', 'title': '新闻', 'items': items},
            {'key': 'events', 'items': [{
                'title': '其他状态徽标', 'facts': ['正在进行', '真实活动说明'],
                'reference': {'kind': 'portal_event'},
            }]},
        ]

    def test_real_daily_and_weekly_render_preserves_content_and_channel_guards(self):
        for report_day, weekly in [(date(2026, 11, 2), False), (date(2026, 11, 3), True)]:
            with self.subTest(weekly=weekly):
                sections = self.sections()
                original = deepcopy(sections)
                document = render_daily_html(report_day, sections)
                soup = BeautifulSoup(document, 'html.parser')
                cards = soup.select('.news-card')
                self.assertEqual(len(cards), len(sections[0]['items']))
                self.assertEqual(len(soup.select('.bluepost-channel-badge')), 8)
                for index, (card, item) in enumerate(zip(cards, sections[0]['items'])):
                    self.assertEqual(card.select_one('.news-title').get_text(), item['title'])
                    self.assertEqual(card.select_one('.news-title a')['href'], item['url'])
                    self.assertEqual(card.select_one('.original-link')['href'], item['url'])
                    self.assertEqual(card.select('.original-link')[1]['href'], item['portal_url'])
                    badge = card.select_one('.bluepost-channel-badge')
                    if index < 8:
                        self.assertIsNotNone(badge)
                        self.assertEqual(badge.name, 'span')
                        self.assertEqual(badge.get_text(), f'[{item["game_version_label"]}]')
                        self.assertEqual(badge['data-game-version'], item.get('game_version') or '')
                        self.assertEqual(set(badge.attrs), {'class', 'data-game-version'})
                        self.assertFalse(badge.find())
                    else:
                        self.assertIsNone(badge)
                    summary = card.select_one('.item-summary')
                    label = 'AI 总结' if item['summary_llm_ok'] else '内容摘要'
                    expected = ' '.join(item['summary'].split()) if weekly else item['summary']
                    self.assertEqual(summary.get_text(), f'{label}：{expected}')
                    self.assertEqual(summary.name, 'p' if weekly else 'div')
                    if weekly:
                        self.assertIsNone(card.select_one('.body-html, .body-text, .original-label'))
                    else:
                        self.assertIn('<div class="body-html">' + item['body_html'] + '</div>', document)
                self.assertEqual(soup.h1.get_text(), '魔兽世界周报' if weekly else '魔兽世界日报')
                self.assertEqual(soup.select_one('.edition-date').get_text(), '2026-11-04' if weekly else '2026-11-02')
                self.assertEqual(sections, original)
                self.assertFalse(soup.select('script, svg, [onmouseover], [onload], link[rel="stylesheet"]'))

    def test_new_editions_inline_the_shared_source_css_verbatim_once(self):
        palette = (Path(__file__).resolve().parents[2] / 'static/shared/css/bluepost-channel.css').read_text(encoding='utf-8')
        for report_day in [date(2026, 11, 2), date(2026, 11, 3)]:
            document = render_daily_html(report_day, self.sections())
            soup = BeautifulSoup(document, 'html.parser')
            self.assertEqual(len([node for node in soup.select('head style') if node.get_text() == palette]), 1)
            self.assertEqual(document.count(palette), 1)
