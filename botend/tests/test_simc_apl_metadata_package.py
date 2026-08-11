import copy
import json
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from botend.models import SimcAplSymbol
from botend.services.simc_apl.metadata_package import (
    build_metadata_package,
    find_default_metadata_package,
    import_metadata_package,
    load_metadata_package,
    validate_metadata_package,
)


REVISION = '8' * 40
BUILD = '12.0.7.68974'


def source_record(spec, token, *, spell_id=None, name_zh='', kind='action'):
    return {
        'class': 'warrior',
        'spec': spec,
        'kind': kind,
        'token': token,
        'spell_id': spell_id,
        'spell_id_candidates': [spell_id] if spell_id else [],
        'sources': ['runtime_action_probe'],
        'identity_reasons': ['运行时解析'],
        'aliases': [],
        'action_options': ['if'] if kind == 'action' else [],
        'expression_suffixes': ['up'] if kind != 'action' else [],
        'name_zh': name_zh,
        'wowhead_status': 'ok' if name_zh else ('unlocalized' if spell_id else 'unbound'),
        'wowhead_raw_name': name_zh,
        'wowhead_url': f'https://www.wowhead.com/cn/spell={spell_id}' if spell_id else '',
        'apl_field': token,
        'apl_expression_template': token,
        'class_zh': '战士',
        'spec_zh': {'arms': '武器', 'fury': '狂怒'}[spec],
        'identity_status': 'bound' if spell_id else 'unbound',
    }


def source_payload():
    return {
        'schema_version': 1,
        'simc_revision': REVISION,
        'game_build': BUILD,
        'generated_at': '2026-08-10T10:42:48+00:00',
        'official_specs': {'warrior': ['arms', 'fury']},
        'expression_suffixes': {'action': [], 'buff': ['up']},
        'wowhead': {'data_env': 1, 'locale': 4, 'environment_scope': 'current'},
        'records': [
            source_record('arms', 'execute', spell_id=5308, name_zh='斩杀'),
            source_record('fury', 'execute', spell_id=5308, name_zh='斩杀'),
            source_record('fury', 'imaginary_buff', kind='buff'),
        ],
    }


class SimcAplMetadataBuilderTests(TestCase):
    def test_builder_prefers_spec_and_only_collapses_proven_class_facts(self):
        package = build_metadata_package(source_payload())

        self.assertEqual(package['counts']['source_record_count'], 3)
        self.assertEqual(package['counts']['fact_count'], 2)
        execute = next(fact for fact in package['facts'] if fact['token'] == 'execute')
        missing = next(fact for fact in package['facts'] if fact['token'] == 'imaginary_buff')
        self.assertEqual((execute['scope'], execute['class_name'], execute['spec']),
                         ('class', 'warrior', None))
        self.assertEqual(execute['name_zh'], '斩杀')
        self.assertEqual((missing['scope'], missing['spec'], missing['name_en']),
                         ('spec', 'fury', 'imaginary_buff'))
        self.assertEqual(missing['name_zh'], '')
        self.assertEqual(missing['localization_status'], 'unbound')

    def test_builder_does_not_collapse_when_localization_differs(self):
        payload = source_payload()
        payload['records'][1]['name_zh'] = '不同名称'
        package = build_metadata_package(payload)
        execute = [fact for fact in package['facts'] if fact['token'] == 'execute']
        self.assertEqual([(fact['scope'], fact['spec']) for fact in execute], [
            ('spec', 'arms'), ('spec', 'fury'),
        ])

    def test_builder_rejects_null_spell_id_candidate(self):
        payload = source_payload()
        payload['records'][0]['spell_id_candidates'] = [None]
        with self.assertRaisesRegex(ValueError, '不能包含 null'):
            build_metadata_package(payload)

    def test_validation_rejects_declared_count_drift(self):
        package = build_metadata_package(source_payload())
        package['counts']['missing_zh_count'] += 1
        with self.assertRaisesRegex(ValueError, 'counts'):
            validate_metadata_package(package)

    def test_generation_is_deterministic(self):
        first = build_metadata_package(source_payload())
        second = build_metadata_package(copy.deepcopy(source_payload()))
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )


class SimcAplMetadataImportTests(TestCase):
    def setUp(self):
        self.package = build_metadata_package(source_payload())

    def test_import_is_idempotent_and_keeps_blank_chinese(self):
        first = import_metadata_package(self.package)
        second = import_metadata_package(self.package)

        self.assertEqual((first.created, first.updated), (2, 0))
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 2))
        missing = SimcAplSymbol.objects.get(token='imaginary_buff')
        self.assertEqual((missing.name_en, missing.name_zh), ('imaginary_buff', ''))
        self.assertEqual(missing.spec, 'fury')
        self.assertEqual(SimcAplSymbol.objects.count(), 2)

    def test_import_dry_run_rolls_back_all_rows(self):
        summary = import_metadata_package(self.package, dry_run=True)
        self.assertEqual(summary.created, 2)
        self.assertEqual(SimcAplSymbol.objects.count(), 0)

    def test_import_preserves_exact_manual_identity(self):
        SimcAplSymbol.objects.create(
            simc_revision=REVISION, wow_build=BUILD, class_name='warrior',
            token='execute', symbol_kind='action', spell_id=999,
            source=SimcAplSymbol.SOURCE_MANUAL, name_zh='人工名称',
        )
        summary = import_metadata_package(self.package)
        manual = SimcAplSymbol.objects.get(token='execute')
        self.assertEqual(summary.manual_preserved, 1)
        self.assertEqual((manual.spell_id, manual.name_zh, manual.source),
                         (999, '人工名称', SimcAplSymbol.SOURCE_MANUAL))

    def test_missing_rows_are_only_deactivated_when_explicit(self):
        stale = SimcAplSymbol.objects.create(
            simc_revision=REVISION, wow_build=BUILD, class_name='warrior', spec='fury',
            token='stale_buff', symbol_kind='buff', is_active=True,
        )
        import_metadata_package(self.package)
        stale.refresh_from_db()
        self.assertTrue(stale.is_active)

        summary = import_metadata_package(self.package, deactivate_missing=True)
        stale.refresh_from_db()
        self.assertEqual(summary.deactivated, 1)
        self.assertFalse(stale.is_active)


class SimcAplBuiltInPackageTests(TestCase):
    EXPECTED_COUNTS = {
        'source_record_count': 8318,
        'fact_count': 4947,
        'official_spec_count': 34,
        'scope_counts': {'class': 1948, 'spec': 2999},
        'kind_counts': {
            'action': 1395,
            'buff': 3352,
            'cooldown': 135,
            'debuff': 30,
            'dot': 35,
        },
        'bound_count': 2004,
        'unbound_count': 2943,
        'localized_count': 4947,
        'missing_zh_count': 0,
    }

    def test_built_in_package_contract_and_full_idempotent_import(self):
        path = find_default_metadata_package()
        self.assertTrue(Path(path).is_file())
        payload = load_metadata_package(path)
        self.assertEqual(payload['counts'], self.EXPECTED_COUNTS)
        self.assertEqual(payload['simc_revision'], '800980b758674a0311a68a6ec33fdc0651afe47d')
        self.assertEqual(payload['game_build'], '12.0.7.68974')
        self.assertTrue(all(fact['name_en'] for fact in payload['facts']))
        self.assertTrue(all(fact['name_zh'] for fact in payload['facts']))
        self.assertFalse(any(fact['scope'] == 'global' for fact in payload['facts']))
        priest_facts = [fact for fact in payload['facts'] if fact['class_name'] == 'priest']
        self.assertTrue(priest_facts)
        self.assertEqual({(fact['scope'], fact['spec']) for fact in priest_facts},
                         {('spec', 'shadow')})

        first = import_metadata_package(payload)
        second = import_metadata_package(payload)
        self.assertEqual(first.created, 4947)
        self.assertEqual(second.unchanged, 4947)
        self.assertEqual(SimcAplSymbol.objects.filter(name_zh='').count(), 0)
        self.assertEqual(SimcAplSymbol.objects.exclude(name_zh='').count(), 4947)

    def test_management_command_dry_run_uses_validated_package(self):
        output = StringIO()
        call_command('import_simc_apl_metadata', dry_run=True, stdout=output)
        text = output.getvalue()
        self.assertIn('[DRY-RUN]', text)
        self.assertIn('facts=4947', text)
        self.assertEqual(SimcAplSymbol.objects.count(), 0)
