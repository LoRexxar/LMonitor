from pathlib import Path
from tempfile import TemporaryDirectory

from io import StringIO
from django.core.management import call_command
from django.test import TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.services.combat_log_visuals import decorate_spells
from botend.templatetags.wow_tags import wow_icon_oss_url


class BuffIconTests(TestCase):
    def test_aura_ids_use_report_then_named_art_then_exact_file_id(self):
        with TemporaryDirectory() as root:
            version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults=dict(
                branch='retail', status='active', is_active=True, source_dir=root))
            Path(root, 'file_data_icon_cache.csv').write_text(
                'FileDataID,IconName\n135940,spell_holy_powerwordshield\n', encoding='utf-8')
            for sid, fdid in [(17, 135940), (10060, 135939), (190319, 135824), (42, 17)]:
                WowTalentNodeMetadata.all_objects.create(talent_version=version, localization_only=True,
                    name_kind='spell', reference_id=sid, icon=f'https://assets-ng.maxroll.gg/wow/icons/{fdid}.webp')
            for sid, entry, slug in [(10060, 103834, 'spell_holy_powerinfusion'),
                                     (190319, 124756, 'spell_fire_sealoffire')]:
                WowTalentNodeMetadata.objects.create(talent_version=version, node_id=entry, spell_id=sid, icon=slug)
            rows = [dict(spell=str(i)) for i in (17, 10060, 190319, 42)]
            decorate_spells(rows, 'spell')
            self.assertEqual([r['icon'] for r in rows], [wow_icon_oss_url(s) for s in
                ('spell_holy_powerwordshield', 'spell_holy_powerinfusion', 'spell_fire_sealoffire')] +
                [None])
            # A pending FileDataID remains discoverable from raw public catalog rows,
            # not from private reports, page views or a speculative OSS key.
            pending = StringIO()
            call_command('list_pending_display_images', stdout=pending)
            self.assertIn('https://assets-ng.maxroll.gg/wow/icons/17.webp', pending.getvalue().splitlines())
            report_art = wow_icon_oss_url('spell_fire_flamebolt')
            self.assertTrue(report_art)
            decorate_spells(rows, 'spell', {10060: report_art})
            self.assertEqual(rows[1]['icon'], report_art)
            with Path(root, 'file_data_icon_cache.csv').open('a') as f:
                f.write('17,spell_fire_flamebolt\n')
            decorate_spells(rows, 'spell')
            self.assertEqual(rows[-1]['icon'], wow_icon_oss_url('spell_fire_flamebolt'))
