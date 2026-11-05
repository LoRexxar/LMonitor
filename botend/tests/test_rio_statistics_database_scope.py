"""使用真实数据库模型验证赛季和版本身份，不依赖上游日期。"""
from django.test import TestCase
from botend.models import SeasonMeta, WowTalentVersion
from botend.services.rio_statistics_scope import active_scope, ScopeUnavailable


class RioDatabaseScopeTests(TestCase):
    def setUp(self):
        SeasonMeta.objects.update(is_active=False)
        WowTalentVersion.objects.update(is_active=False)
        self.season = SeasonMeta.objects.create(season_key='mn-s2-scope', season_name='午夜第二赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        self.version = WowTalentVersion.objects.create(key='retail-scope', branch='retail', is_active=True,
            current_build='12.1.5.70000')

    def test_database_season_is_authority_and_build_change_invalidates_identity(self):
        before = active_scope()
        self.assertEqual(before['rio_season'], 'season-mn-2')
        self.assertEqual(before['expansion_id'], 11)
        self.season.game_build = '12.1.5.70001'
        self.season.save(update_fields=['game_build'])
        self.assertNotEqual(active_scope()['token'], before['token'])

    def test_central_version_change_invalidates_even_when_season_unchanged(self):
        before = active_scope()
        self.version.current_build = '12.1.5.70001'
        self.version.save(update_fields=['current_build'])
        self.assertNotEqual(active_scope()['token'], before['token'])

    def test_missing_binding_never_chooses_latest_rio_season(self):
        self.season.rio_season = ''
        self.season.save(update_fields=['rio_season'])
        with self.assertRaises(ScopeUnavailable): active_scope()

    def test_multiple_active_seasons_are_not_resolved_by_first_row(self):
        SeasonMeta.objects.create(season_key='other', season_name='另一个赛季', rio_season='season-mn-3',
            game_build='12.2.0.80000', is_active=True, mplus_zone_id=2, raid_zone_id=3)
        with self.assertRaises(ScopeUnavailable): active_scope()
