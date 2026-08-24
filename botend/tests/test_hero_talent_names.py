from django.test import SimpleTestCase

from botend.constants.hero_talents import (
    HERO_SUBTREE_ID_TO_NAME, HERO_SUBTREE_NAME_ZH, spec_hero_subtree_names,
)


class HeroTalentNameTests(SimpleTestCase):
    def test_names_match_current_wowhead_zhcn_hero_tree_catalog(self):
        # https://nether.wowhead.com/cn/data/talents-dragonflight
        expected = {
            'Aldrachi Reaver': '奥达奇收割者',
            'Annihilator': '歼灭者',
            'Archon': '执政官',
            'Chronowarden': '时空守卫',
            'Colossus': '巨神兵',
            'Conduit of the Celestials': '天神御师',
            'Dark Ranger': '黑暗游侠',
            'Deathbringer': '死亡使者',
            'Deathstalker': '死亡猎手',
            'Diabolist': '恶魔使徒',
            'Druid of the Claw': '利爪德鲁伊',
            "Elune's Chosen": '艾露恩钦选者',
            'Farseer': '先知',
            'Fatebound': '命缚者',
            'Fel-Scarred': '邪痕枭雄',
            'Flameshaper': '塑焰者',
            'Frostfire': '霜火',
            'Hellcaller': '地狱召唤者',
            'Herald of the Sun': '烈日先驱',
            'Keeper of the Grove': '丛林守护者',
            'Lightsmith': '铸光者',
            'Master of Harmony': '祥和宗师',
            'Mountain Thane': '山丘领主',
            'Oracle': '神谕者',
            'Pack Leader': '猎群领袖',
            'Rider of the Apocalypse': '天启骑士',
            "San'layn": '萨莱因',
            'Scalecommander': '鳞长',
            'Sentinel': '哨兵',
            'Shado-Pan': '影踪派',
            'Slayer': '屠戮者',
            'Soul Harvester': '灵魂收割者',
            'Spellslinger': '疾咒师',
            'Stormbringer': '风暴使者',
            'Sunfury': '日怒',
            'Templar': '圣殿骑士',
            'Totemic': '图腾祭司',
            'Trickster': '欺诈者',
            'Void-Scarred': '虚痕枭雄',
            'Voidweaver': '虚空编织者',
            'Wildstalker': '荒野追猎者',
        }

        self.assertEqual(HERO_SUBTREE_NAME_ZH, expected)

    def test_midnight_demon_hunter_subtrees_and_spec_relationships_are_authoritative(self):
        # Wago TraitSubTree build 12.1.0.69404 and Wowhead 12.1.0 spec guides.
        self.assertEqual(HERO_SUBTREE_ID_TO_NAME[124], 'Annihilator')
        self.assertEqual(HERO_SUBTREE_ID_TO_NAME[126], 'Void-Scarred')
        self.assertEqual(
            spec_hero_subtree_names('DemonHunter', 'Havoc'),
            ('Aldrachi Reaver', 'Fel-Scarred'),
        )
        self.assertEqual(
            spec_hero_subtree_names('DemonHunter', 'Vengeance'),
            ('Annihilator', 'Aldrachi Reaver'),
        )
        self.assertEqual(
            spec_hero_subtree_names('DemonHunter', 'Devourer'),
            ('Annihilator', 'Void-Scarred'),
        )
