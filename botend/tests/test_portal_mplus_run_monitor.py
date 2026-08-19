from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalMplusRunMonitor import PortalMplusRunMonitor
from botend.constants.wow import RAID_BOSS_CN, RAID_ZONE_CN
from botend.portal.api import _mplus_to_dict
from botend.services.spec_stats_service import _lookup_dungeon_cn
from botend.wow_i18n import cn_dungeon_from_slug


class PortalMplusRunMonitorSeasonTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.PortalMplusRunMonitor.SeasonMeta.objects.filter')
    def test_uses_active_season_metadata_instead_of_s1_literal(self, season_filter):
        season_filter.return_value.first.return_value = SimpleNamespace(rio_season='season-mn-2')
        monitor = PortalMplusRunMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-2')
        season_filter.assert_called_once_with(is_active=True)

    def test_top_run_uses_chinese_dungeon_name_from_shared_catalog(self):
        run = SimpleNamespace(
            rank=1,
            dungeon='Voidscar Arena',
            dungeon_slug='voidscar-arena',
            level=22,
            time_seconds=1200,
            score=400.0,
            party_json=None,
            dps_json=None,
            tank='',
            healer='',
            run_url='',
            source='raiderio',
            season='season-mn-2',
            region='world',
        )

        self.assertEqual(_mplus_to_dict(run)['dungeon_cn'], '虚空之痕竞技场')
        self.assertEqual(
            {
                slug: cn_dungeon_from_slug(slug, english)
                for slug, english in {
                    'altar-of-fangs': 'Altar of Fangs',
                    'den-of-nalorakk': 'Den of Nalorakk',
                    'kings-rest': "Kings' Rest",
                    'murder-row': 'Murder Row',
                    'ruby-life-pools': 'Ruby Life Pools',
                    'temple-of-sethraliss': 'Temple of Sethraliss',
                    'the-blinding-vale': 'The Blinding Vale',
                    'voidscar-arena': 'Voidscar Arena',
                }.items()
            },
            {
                'altar-of-fangs': '毒牙祭坛',
                'den-of-nalorakk': '纳洛拉克的洞穴',
                'kings-rest': '诸王之眠',
                'murder-row': '密谋小径',
                'ruby-life-pools': '红玉新生法池',
                'temple-of-sethraliss': '塞塔里斯神庙',
                'the-blinding-vale': '夺目谷',
                'voidscar-arena': '虚空之痕竞技场',
            },
        )

    def test_current_season_names_match_wago_zhcn_db2(self):
        self.assertEqual(
            {
                name: _lookup_dungeon_cn(name)
                for name in (
                    'Altar of Fangs',
                    'Den of Nalorakk',
                    "Kings' Rest",
                    'Murder Row',
                    'Ruby Life Pools',
                    'Temple of Sethraliss',
                    'The Blinding Vale',
                    'Voidscar Arena',
                )
            },
            {
                'Altar of Fangs': '毒牙祭坛',
                'Den of Nalorakk': '纳洛拉克的洞穴',
                "Kings' Rest": '诸王之眠',
                'Murder Row': '密谋小径',
                'Ruby Life Pools': '红玉新生法池',
                'Temple of Sethraliss': '塞塔里斯神庙',
                'The Blinding Vale': '夺目谷',
                'Voidscar Arena': '虚空之痕竞技场',
            },
        )
        self.assertEqual(RAID_ZONE_CN['The Venomous Abyss'], '烈毒之渊')
        self.assertEqual(
            {
                name: RAID_BOSS_CN[name]
                for name in (
                    "Nek'zali the Soulcoiler",
                    'Entombed Sentinels',
                    'Vashnik the Malignant',
                    'The Lost Explorers',
                    'Sszorak',
                    'The Twin Fangs',
                    'The Coiled Altar',
                    "Ula'tek",
                    'Nymrissa Wavecaller',
                )
            },
            {
                "Nek'zali the Soulcoiler": '盘魂者内克扎莉',
                'Entombed Sentinels': '陵寝哨兵',
                'Vashnik the Malignant': '万毒邪祟者瓦什尼克',
                'The Lost Explorers': '迷失的探险者',
                'Sszorak': '斯索拉克',
                'The Twin Fangs': '双子毒牙',
                'The Coiled Altar': '盘卷祭坛',
                "Ula'tek": '乌拉特克',
                'Nymrissa Wavecaller': '尼姆瑞莎·唤波者',
            },
        )
