"""Offline artifact -> real ORM -> catalog recovery; no mocked core services."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase

from botend.management.commands.sync_gear_builder_catalog import Command as CatalogImporter
from botend.models import WowItemVariantSnapshot
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource
from botend.services.kithix_localization import import_localization
from botend.services.ptr_journal_gear_overlay import import_ptr_journal_gear_overlay
from botend.services.wow_data_update import variant_payload
from botend.tests import test_ptr_journal_gear_overlay as overlay_tests


class EffectLocalizationRegressionTests(TestCase):
    def setUp(self):
        overlay_tests.PtrJournalGearOverlayTests.setUp(self)
        self.artifact = Path(settings.BASE_DIR) / 'botend/data/ptr_kithix_unbound_12_1_5.json'
        self.localization = self.artifact.with_name('kithix_localization_12_1_5.json')
        import_ptr_journal_gear_overlay(self.artifact, apply=True)
        self.temp = tempfile.TemporaryDirectory(prefix='effects-regression-')
        self.addCleanup(self.temp.cleanup)
        self.backup = Path(self.temp.name) / 'before.json'
        self.row = WowItemVariantSnapshot.objects.select_related('item').get(
            item__item_id=281029, variant_key='ptr-kithix-champion-1')
        self.original = deepcopy(self.row.effects_json)

    def apply(self):
        return import_localization(self.localization, apply=True, backup_path=self.backup)

    def test_daily_publish_preserves_structured_facts_and_accepts_new_native_values(self):
        from types import SimpleNamespace
        from botend.models import WowDataUpdateRun, WowDataUpdateState
        from botend.services.wow_data_update import item_payload, publish, update_lease
        self.apply()
        self.row.refresh_from_db()
        original = deepcopy(self.row.effects_json)
        state, _ = WowDataUpdateState.objects.get_or_create(branch='ptr')
        value = variant_payload(self.row)
        item = item_payload(self.row.item)
        item['variants'] = [value]
        bundle = {'catalog': {'game_build': self.row.game_build, 'items': [item]},
                  'season_id': self.season.pk, 'previous_batch': self.season.gear_batch_key}
        version = SimpleNamespace(label='PTR test', major_version='12.1.5',
            current_build=self.row.game_build, source_dir='', granted_entries_json={}, _prepared_nodes=[])
        for incoming, expected in (([{'description': 'Equip: identityless daily update'}], original),
                                   ([{**original[0], 'description': 'authoritative native update',
                                      'description_zh': '新权威中文', 'game_build': '12.1.5.99999'}], None)):
            value['effects'] = incoming
            run = WowDataUpdateRun.objects.create(branch='ptr', build=self.row.game_build, report={})
            with update_lease() as (task, heartbeat):
                publish(bundle, version, state, run, task)
            self.row.refresh_from_db()
            self.assertEqual(self.row.effects_json, expected if expected is not None else incoming)
            run.refresh_from_db()
            self.assertEqual(run.status, 'published')

    def test_cached_tooltip_and_real_catalog_upsert_preserve_and_accept_new_facts(self):
        self.apply()
        self.row.refresh_from_db()
        trusted = deepcopy(self.row.effects_json)
        source = CurrentGearCatalogSource(cache_dir=self.temp.name)
        cache = source._tooltip_cache_directory(self.row.game_build)
        cache.mkdir(parents=True)
        (cache / f'{self.row.item.item_id}-{self.row.item_level}.json').write_text(json.dumps({
            'name': self.row.item.name, 'quality': 4,
            'tooltip': '<!--ilvl-->292<br>Equip: unbound tooltip text'}))
        value = variant_payload(self.row)
        source._enrich_wowhead([{'item_id': self.row.item.item_id, 'variants': [value]}], self.row.game_build)
        self.assertEqual(value['effects'], trusted)
        importer = CatalogImporter()
        for incoming, expected in (([{'description': 'Equip: unbound tooltip text'}], trusted),
                                   ([{**trusted[0], 'description': 'new native fact',
                                      'description_zh': '新权威事实', 'game_build': '12.1.5.99999'}], None)):
            value['effects'] = incoming
            result = importer._upsert_variant(season=self.season, batch_key=self.season.gear_batch_key,
                item=self.row.item, variant_key=self.row.variant_key,
                defaults=importer._variant_defaults(value, self.row.game_build))
            result.refresh_from_db()
            self.assertEqual(result.effects_json, expected if expected is not None else incoming)


class DifficultyImmutabilityRegressionTests(TestCase):
    def test_localization_marker_cannot_authorize_frozen_fact_mutation(self):
        from botend.tests.test_wow_item_difficulty_import import DifficultyImportTests
        from botend.services.wow_item_difficulty_import import import_difficulty_variant
        DifficultyImportTests.setUp(self)
        result = import_difficulty_variant(**self.kw, apply=True)
        row = WowItemVariantSnapshot.objects.get(pk=result['variant_id'])
        row.effects_json = [{'description_zh': '伪造新效果', 'localization_build': '12.1.5.70077'}]
        row.save(update_fields=['effects_json'])
        with self.assertRaisesRegex(ValidationError, 'conflicting dedicated variant facts'):
            import_difficulty_variant(**self.kw)
        with self.assertRaisesRegex(ValidationError, 'conflicting dedicated variant facts'):
            import_difficulty_variant(**self.kw, apply=True)
