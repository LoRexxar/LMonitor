"""Choice display regression: real ORM, code-only decode, shared render and SSR."""
import copy
from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import TestCase
from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.services.simc_reference_display import reference_trees
from botend.wow.talents.build_code import TalentBuildCodeEncoder
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.service import TalentBuildCodeService
from botend.wow.talents.view_model import build_talent_view_model


class ReferenceChoiceDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version = WowTalentVersion.objects.create(key='choice-display-test', branch='retail', is_active=True)
        for index in range(2):
            WowTalentNodeMetadata.objects.create(
                talent_version=cls.version, class_name='Warrior', spec_name='Fury', tree_type='class',
                node_id=710001 + index, talent_id=810001, spell_id=910001 + index,
                name=f'Option {index}', name_zh=f'选项{index}', icon=f'choice_icon_{index}',
                description=f'English description {index}', description_zh=f'中文说明{index}',
                row=1, column=1, max_points=1, source='db2',
            )
        cls.spec = {'key': 'warrior_fury', 'class_name': 'Warrior', 'spec_name': 'Fury'}

    def markup(self, display):
        return BeautifulSoup(render_to_string('portal/_simc_reference_trees.html', {'talent_display': display}), 'html.parser')

    def test_code_only_choice_fields_and_single_active_option(self):
        provider = TalentMetadataProvider(talent_version=self.version)
        universe = provider.get_decoder_node_list('Warrior')
        base = provider.get_full_tree_nodes('Warrior', 'Fury')[0]
        self.assertEqual(len(base['choice_options']), 2)
        original = copy.deepcopy(base)
        header = 'CgEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgGDjxMsMzMzMDjZmZGzMzsMzMGzMbDzMAAQMWWGYBMBzwEYG2AmZ2Y2GAAMzYYMzMMYA'
        for selected_index in (1, 0, None):
            with self.subTest(selected_index=selected_index):
                selected = [] if selected_index is None else [{**base, 'selected': True, 'purchased': True, 'points': 1, 'is_choice_node': True, 'choice_selection': selected_index}]
                code = TalentBuildCodeEncoder.encode_node_states(header, universe, selected)
                self.assertTrue(code)
                display = reference_trees(code, self.spec, self.version)
                self.assertFalse(display['message'])
                node = next(n for tree in display['trees'] for n in tree['nodes'] if n.get('choice_options'))
                card = self.markup(display).select_one('.is-choice-node')
                self.assertIsNotNone(card)
                active = card.select('.talent-choice-popup-option.is-active')
                if selected_index is None:
                    self.assertFalse(node['selected'])
                    self.assertEqual(active, [])
                    self.assertEqual(len(card.select('.talent-icon-split-half')), 2)
                    continue
                option = base['choice_options'][selected_index]
                self.assertEqual(node['choice_selection'], selected_index)
                for field in ('name', 'icon', 'description', 'description_zh', 'spell_id', 'display_spell_id'):
                    self.assertEqual(node[field], option[field], field)
                self.assertEqual(node['node_id'], base['node_id'])
                self.assertEqual(card['href'], f"https://www.wowhead.com/spell={option['spell_id']}")
                self.assertEqual(card.select_one('.talent-node-icon img')['alt'], option['name'])
                self.assertEqual(card.select_one('.talent-node-tooltip')['data-tooltip-desc'], option['description_zh'])
                self.assertEqual([x['data-option-name'] for x in active], [option['name']])
        self.assertEqual(base, original)
        self.assertEqual(list(WowTalentNodeMetadata.objects.filter(talent_version=self.version).order_by('node_id').values_list('name_zh', flat=True)), ['选项0', '选项1'])

    def test_missing_selected_text_does_not_inherit_first_option(self):
        base = TalentMetadataProvider(talent_version=self.version).get_full_tree_nodes('Warrior', 'Fury')[0]
        base['choice_options'][1].update(name='', icon='', description='', description_zh='')
        key = f"{base['tree_type']}:{base['node_id']}"
        merged = TalentBuildCodeService._merge_full_tree_nodes([base], [], decoded_states={key: {
            'selected': True, 'purchased': True, 'points': 1, 'is_choice_node': True, 'choice_selection': 1,
        }}, has_build_code=True)
        for field in ('name', 'icon', 'description', 'description_zh'):
            self.assertEqual(merged[0][field], '', field)

    def test_legacy_selected_spell_is_unique_and_unknown_choice_is_not_guessed(self):
        base = TalentMetadataProvider(talent_version=self.version).get_full_tree_nodes('Warrior', 'Fury')[0]
        option = base['choice_options'][1]
        base.update(selected=True, points=1, choice_selection=None, spell_id=option['spell_id'], display_spell_id=option['display_spell_id'])
        for explicit in (None, 5):
            with self.subTest(explicit=explicit):
                base['choice_selection'] = explicit
                model = build_talent_view_model([base], class_name='Warrior', spec_name='Fury', metadata_provider=TalentMetadataProvider(talent_version=self.version))['render_model']
                active = self.markup({'render_model': model}).select('.talent-choice-popup-option.is-active')
                self.assertEqual([x['data-option-name'] for x in active], [option['name']] if explicit is None else [])
