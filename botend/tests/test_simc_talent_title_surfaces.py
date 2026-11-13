"""Presentation must not change executable talent identity."""
import hashlib
import json
from django.test import TestCase
from django.template.loader import render_to_string
from botend.guide_models import ClassGuide
from botend.models import SimcTalentString, SimcReferencePublication
from botend.services.simc_reference import reference_catalog
from botend.services.simc_task_service import _build_talent_payload, _compute_content_hash

CODE = 'C8DAAAAAAAAAAAAAAAAAAAAAAYGGLzMzsgZGZmZGAAAMLzYmZZWAAAALmZYbmZmZsBAAAAgNzMmZAAwMDzMmZmZWAmZANjxYwMMGA'


class TalentTitleSurfaceTests(TestCase):
    def setUp(self):
        self.guide = ClassGuide.objects.create(
            slug='fire-title-test', title='Fire Mage', spec_id=63,
            class_name='Mage', spec_name='Fire', game_version='12.1.5',
            source_url='https://www.icy-veins.com/wow/fire-mage-pve-dps-guide',
            source_markdown='## 大秘境\n\n### Frostfire Mythic+ Talents\n\n:::talents 天赋构筑\n```json\n' + json.dumps({'code': CODE}) + '\n```\n:::\n',
        )
        suffix = hashlib.sha256(CODE.encode()).hexdigest()
        self.talent = SimcTalentString.objects.create(
            name='[Icy Veins] 霜火 · ![](<https://static.icy-veins.com/frostfire.webp>) Frostfire Mythic+ Talents · ' + suffix[:8],
            spec='mage_fire', talent=CODE, hero_talent_names=['霜火'], is_system=True,
            system_key=f'guide:icy-veins:{self.guide.pk}:mage_fire:{suffix}',
        )

    def test_catalog_and_publication_names_are_display_only(self):
        original = _build_talent_payload(self.talent)
        signature = _compute_content_hash(original)
        _, specs = reference_catalog(public_only=False)
        row = specs['mage_fire']['talents'][0]
        title = row.get('display_name', row['name'])
        self.assertNotIn('https://', title)
        self.assertIn('大秘境', title)
        self.assertIn('Frostfire', title)
        self.assertEqual(row['name'], original['name'])
        from botend.dashboard.simc_reference_results import PublicationForm
        from botend.services.simc_reference_workflow import publication_sections
        publication = SimcReferencePublication.objects.create(spec_key='mage_fire')
        form = PublicationForm(instance=publication)
        sections = publication_sections(form, None, {'can_edit': False, 'editor_urls': {}})
        item = next(s for s in sections if s['kind'] == 'talent')['choices'][0]
        self.assertEqual(item['resource'].display_name, title)
        self.assertEqual(str(item['choice'].choice_label), title)
        html = render_to_string('dashboard/simc_reference_resource_detail.html', {
            'resource': item['resource'], 'preview_url': '/preview/', 'resource_label': '天赋',
        })
        self.assertNotIn('https://static.icy-veins.com', html)
        self.assertIn('Frostfire', html)
        self.talent.refresh_from_db()
        self.assertEqual(_build_talent_payload(self.talent), original)
        self.assertEqual(_compute_content_hash(_build_talent_payload(self.talent)), signature)


class TalentTitleRankingTests(TestCase):
    from botend.tests.test_simc_reference_rankings import ReferenceRankingTests as _fixture
    setUp = _fixture.setUp
    result = _fixture.result
    other_spec = _fixture.other_spec

    def test_display_keeps_success_current_but_real_code_change_invalidates(self):
        from botend.services.simc_talent_titles import apply_talent_display_names
        from botend.services.simc_reference_rankings import leaderboard
        from botend.services.simc_reference_ranking_index import refresh_configs
        from botend.models import SimcReferenceRankingEntry
        other = self.other_spec()
        talent = other['talent']
        guide = ClassGuide.objects.create(
            slug='fire-ranking-title-test', title='Fire Mage', spec_id=63,
            class_name='Mage', spec_name='Fire', game_version='12.1.5',
            source_url='https://www.icy-veins.com/wow/fire-mage-pve-dps-guide',
            source_markdown='## 大秘境\n\n:::talents Frostfire Mythic+ Talents\n```json\n' + json.dumps({'code': CODE}) + '\n```\n:::\n',
        )
        suffix = hashlib.sha256(CODE.encode()).hexdigest()
        talent.name = '[Icy Veins] 霜火 · ![](<https://static.icy-veins.com/frostfire.webp>) Frostfire Mythic+ Talents · ' + suffix[:8]
        talent.talent = CODE
        talent.hero_talent_names = ['霜火']
        talent.system_key = f'guide:icy-veins:{guide.pk}:mage_fire:{suffix}'
        talent.save()
        result = self.result(123.0, **other)
        frozen = json.dumps(result.snapshot, sort_keys=True)
        apply_talent_display_names([talent])
        refresh_configs([other['config'].pk])
        board = leaderboard(other['config'], result.signature, 'spec')
        self.assertIsNotNone(board)
        self.assertIn('大秘境', board['current']['talent'])
        self.assertNotIn('https://', board['current']['talent'])
        self.assertTrue(SimcReferenceRankingEntry.objects.get(result=result).is_current)
        result.refresh_from_db()
        self.assertEqual(json.dumps(result.snapshot, sort_keys=True), frozen)
        talent.talent = 'changed-real-input'
        talent.save(update_fields=['talent'])
        refresh_configs([other['config'].pk])
        self.assertFalse(SimcReferenceRankingEntry.objects.get(result=result).is_current)
