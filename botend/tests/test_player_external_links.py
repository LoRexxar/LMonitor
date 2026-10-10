import json
from types import SimpleNamespace
from urllib.parse import quote

from django.template.loader import render_to_string
from django.test import TestCase

from botend.models import WowRealmDirectory
from botend.services.player_external_links import build_player_external_links, parse_realm_directory


class PlayerExternalLinksTests(TestCase):
    def test_rendered_character_buttons_use_canonical_regional_realms(self):
        cases = [
            ('eu', 'hyjal', 'Hyjal', '', 'fr_FR', 'Aghistør', 'hyjal', 'en-gb'),
            ('cn', 'silver-hand', 'Silver Hand', '白银之手', 'zh_CN', '童心未泯灬', '白银之手', None),
            ('tw', 'shadowmoon', 'Shadowmoon', '暗影之月', 'zh_TW', '測試', '暗影之月', 'zh-tw'),
            ('kr', 'azshara', 'Azshara', '아즈샤라', 'ko_KR', '테스트', '아즈샤라', 'ko-kr'),
            ('eu', 'howling-fjord', 'Howling Fjord', 'Ревущий фьорд', 'ru_RU', 'Тест', 'ревущии-фьорд', 'en-gb'),
        ]
        for region, slug, name, localized, locale, character, wcl_slug, armory_locale in cases:
            with self.subTest(region=region, slug=slug):
                WowRealmDirectory.objects.create(region=region, slug=slug, name=name, localized_name=localized, locale=locale)
                url=f'https://raider.io/characters/{region}/{slug}/{quote(character, safe="")}'
                player=SimpleNamespace(region=region, realm=name, character_name=character, profile_url=url)
                links=build_player_external_links(player)
                self.assertEqual([x['key'] for x in links], ['raiderio','armory','warcraftlogs','raidbots'])
                self.assertEqual(links[0]['url'],url)
                suffix=f'{quote(wcl_slug, safe="")}/{quote(character, safe="")}'
                self.assertEqual(links[2]['url'],f'https://www.warcraftlogs.com/character/{region}/{suffix}')
                if region=='cn':
                    expected=f'https://wow.blizzard.cn/character/#/{slug}/{quote(character, safe="")}'
                else:
                    armory_slug=slug
                    expected=f'https://worldofwarcraft.com/{armory_locale}/character/{region}/{quote(armory_slug, safe="")}/{quote(character, safe="")}'
                self.assertEqual(links[1]['url'],expected)
                html=render_to_string('portal/spec_detail/_player_external_links.html',{'external_links':links})
                self.assertEqual(html.count('target="_blank"'),4)
                self.assertEqual(html.count('rel="noopener noreferrer"'),4)
                self.assertIn('暴雪英雄榜',html)
                self.assertIn('WCL',html)

    def test_missing_identity_and_untrusted_urls_are_not_guessed(self):
        WowRealmDirectory.objects.create(region='eu',slug='hyjal',name='Hyjal',locale='fr_FR')
        player=SimpleNamespace(region='eu',realm='Hyjal',character_name='Aghistør',profile_url='')
        self.assertEqual(len(build_player_external_links(player)),4)
        player.realm='Unknown realm'
        self.assertEqual(build_player_external_links(player),[])
        player.profile_url='javascript:alert(1)'
        self.assertEqual(build_player_external_links(player),[])
        player.profile_url='https://raider.io/characters/us/hyjal/SomeoneElse'
        self.assertEqual(build_player_external_links(player),[])
        player.profile_url='https://raider.io/characters/eu/anonymous/Anon123'
        player.character_name='Anon123'
        self.assertEqual([x['key'] for x in build_player_external_links(player)],['raiderio'])

    def test_directory_parser_validates_region_and_duplicate_identity(self):
        row={'slug':'silver-hand','name':'Silver Hand','alt_name':'白银之手','locale':'zh_CN'}
        listing={'region':{'slug':'cn'},'realms':[{'region':{'slug':'cn'},'connectedRealms':[row]}]}
        def html():
            return '<script>window.__RIO_INITIAL_DATA = '+json.dumps(json.dumps({'realmListing':listing}))+';</script>'
        records=parse_realm_directory(html(),'cn')
        self.assertEqual(records[0]['localized_name'],'白银之手')
        with self.assertRaises(ValueError): parse_realm_directory(html(),'eu')
        listing['realms'][0]['connectedRealms'].append(dict(row))
        with self.assertRaises(ValueError): parse_realm_directory(html(),'cn')
