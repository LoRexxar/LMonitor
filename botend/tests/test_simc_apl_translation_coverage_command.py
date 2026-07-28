import io
import json
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from botend.models import SimcApl


REVISION = 'c' * 40
BUILD = '12.0.7.68453'


class SimcAplTranslationCoverageCommandTests(TestCase):
    def test_reports_all_six_typed_demands_and_excludes_control_actions(self):
        SimcApl.objects.create(
            name='Fire',
            spec='mage_fire',
            class_name='mage',
            content=(
                'actions=/fireball,if=buff.hot_streak.up&debuff.mark.up&'
                'dot.ignite.ticking&cooldown.combustion.ready&talent.pyromaniac\n'
                'actions+=/apply_poison\n'
            ),
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
            sync_version=REVISION,
        )
        mapped = [
            ('action', 'fireball', '火球术'),
            ('buff', 'hot_streak', '炽热连击'),
            ('dot', 'ignite', '点燃'),
            ('cooldown', 'combustion', '燃烧'),
            ('talent', 'pyromaniac', '纵火狂'),
        ]
        output = io.StringIO()
        with mock.patch(
                'botend.management.commands.audit_simc_apl_translation_coverage.'
                '_latest_catalog_identity', return_value=(REVISION, BUILD)), mock.patch(
                'botend.management.commands.audit_simc_apl_translation_coverage.'
                'ConvertTextAPIView.bilingual_pairs', return_value=(mapped, mapped)):
            call_command('audit_simc_apl_translation_coverage', stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload['apl_count'], 1)
        self.assertEqual(payload['overall'], {
            'control': 1,
            'coverage_pct': 83.33,
            'demand': 6,
            'mapped': 5,
            'missing': 1,
        })
        self.assertEqual(payload['by_kind']['debuff']['missing'], 1)
        self.assertEqual(payload['missing_top']['debuff'], [['mark', 1]])
        self.assertEqual(payload['by_kind']['action']['control'], 1)
