"""Public-cache projection contracts; real ORM and product JavaScript, no network.

The ordinary rows are labelled synthetic ORM contract fixtures. Exact identity
uses the repository's captured Item/ItemSparse evidence. The Node harness only
supplies DOM plumbing; filtering, tooltip, sources, share and SimC are real code.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

from django.test import SimpleTestCase, TestCase

from botend.models import WowItemSnapshot, WowItemVariantSnapshot, SeasonMeta
from botend.services import gear_builder as gb
from botend.services.gear_catalog_projection import compact_catalog_content
from botend.tests.test_gear_builder import GearBuilderTestDataMixin
from botend.tests import test_wow_item_identity as identity_fixture

ROOT = Path(__file__).resolve().parents[2]

# This is deliberately independent of the production allowlist.
DISPLAY_METADATA = {
    'data_branch': 'ptr', 'ptr_preview': True, 'venomstone': True,
    'primary_stat_amount': 900, 'two_handed': True, 'item_set_id': 1919,
    'enchantment_id': 8001,
}
RAW_METADATA = {
    'difficulty_rule_evidence': {'rows': [{'ID': 123}]},
    'simc_effect_activation_by_build': {'12.1.0.99999': {'raw': [1, 2]}},
    'future_unreviewed_raw_fact': {'do_not_copy': True},
}

# Use node -e so this test owns no additional repository files.
NODE_PRESENTATION = r'''
const fs = require('node:fs'), vm = require('node:vm');
const nodes = new Map();
const node = () => ({replaceChildren(...children) {this.children = children;}});
const context = {document: {
  querySelector: () => ({dataset: {}}),
  getElementById: id => {if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id);},
  createElement: tag => tag === 'template' ? {content: {firstElementChild: {isConnected: true}}} : node(),
  addEventListener() {},
}, window: {addEventListener() {}}};
vm.runInNewContext(fs.readFileSync('static/shared/js/wow-gear-sources.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/shared/js/wow-item-tooltip.js', 'utf8'), context);
const source = fs.readFileSync('static/portal/js/gear_builder.js', 'utf8');
vm.runInNewContext(source.replace('  initialize();', `globalThis.qa = {
  tooltipText, variantLabel, sourceText, shortSourceText, socketCapacity,
  simcEquipmentLine, compactShareState, previewSlotMarkup, craftingFields,
  configure: value => {bootstrap = value;}, equip: (slot, entry) => {
    state.equipment = {[slot]: entry}; state.selectedSlot = slot;
  }
};`), context);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
context.qa.configure(input.bootstrap || {slots: [{key: 'head', label: '头部'}]});
const result = input.items.flatMap(item => (item.variants || []).map(variant => {
  item = {...item, ...variant._item};
  const slot = item.slot || 'head';
  const entry = {item, variant, selectedStats: (variant.crafting_options?.stat_pool || []).slice(0, 2), gems: []};
  context.qa.equip(slot, entry);
  const description = context.qa.tooltipText(item, variant), rendered = node();
  context.window.WowItemTooltip.renderContent(rendered, {name: item.name, description});
  return {id: variant.id, key: variant.key, itemId: item.item_id,
    tooltip: rendered.children.map(c => [c.className, c.textContent]),
    label: context.qa.variantLabel(variant), source: context.qa.sourceText(variant),
    shortSource: context.qa.shortSourceText(variant), sockets: context.qa.socketCapacity(entry),
    preview: context.qa.previewSlotMarkup(slot), crafting: context.qa.craftingFields(entry),
    simc: context.qa.simcEquipmentLine(slot, entry),
    share: context.qa.compactShareState({className: 'Warrior', specName: 'Fury', batchKey: 'oracle', equipment: {[slot]: entry}})};
}));
process.stdout.write(JSON.stringify(result));
'''


def presentation(items, bootstrap=None):
    proc = subprocess.run(['node', '-e', NODE_PRESENTATION], cwd=ROOT,
                          input=json.dumps({'items': items, 'bootstrap': bootstrap}),
                          text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


def without_private_facts(value):
    """Compare every protocol field except the intentionally projected containers."""
    if isinstance(value, list):
        return [without_private_facts(row) for row in value]
    if isinstance(value, dict):
        return {key: without_private_facts(row) for key, row in value.items()
                if key not in ('metadata', 'item_identity')}
    return value


def filtered(payload, options):
    proc = subprocess.run(['node', 'botend/tests/js/gear-catalog-filter-oracle.mjs'],
                          cwd=ROOT, input=json.dumps([{'payload': payload, 'options': o} for o in options]),
                          text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


class CatalogProjectionTests(SimpleTestCase):
    def test_allowlist_covers_nested_overrides_and_identity_without_aliases(self):
        ref = {'schema_version': 1, 'item_id': 281235, 'game_build': identity_fixture.NEW,
               'is_ptr': True, 'inventory_type': 7, 'slot_key': 'legs', 'item_class_id': 4,
               'item_subclass_id': 1, 'allowable_class_mask': -1, 'name': 'Leggings',
               'name_zh': '', 'primary_stat_options': ['intellect'], 'quality': 4,
               'fact_hash': 'a' * 64}
        variant = {'id': 9, 'key': 'exact-build', 'metadata': {**DISPLAY_METADATA, **RAW_METADATA},
                   'item_identity': {**ref, 'source': {'evidence': [RAW_METADATA]}},
                   '_item': {'name': 'Override', 'metadata': {**DISPLAY_METADATA, **RAW_METADATA}},
                   'stats': {'intellect': 900}, 'bonus_ids': [123], 'socket_types': ['prismatic'],
                   'effects': [{'description_zh': '装备：效果。'}]}
        item = {'item_id': 281235, 'metadata': {**DISPLAY_METADATA, **RAW_METADATA}, 'variants': [variant]}
        content = {'items': [item], 'groups': {'gems': [item]}, 'embellishment_options': {'9': item},
                   'embellishments_by_equipment': {'8': ['9']}, 'source_groups': {'raid': ['raid']}}
        before = deepcopy(content)
        projected = compact_catalog_content(content)
        for row in (projected['items'][0], projected['groups']['gems'][0], projected['embellishment_options']['9']):
            self.assertEqual(row['metadata'], {})  # compactItem never reads item metadata
            v = row['variants'][0]
            self.assertEqual(v['metadata'], DISPLAY_METADATA)
            self.assertEqual(v['_item']['metadata'], {})
            self.assertEqual(v['item_identity'], ref)
        self.assertEqual(without_private_facts(projected), without_private_facts(content))
        self.assertEqual(compact_catalog_content(projected), projected)
        projected['items'][0]['variants'][0]['stats']['intellect'] = 1
        projected['items'][0]['variants'][0]['item_identity']['primary_stat_options'].append('strength')
        self.assertEqual(content, before)

    def test_preserves_empty_missing_null_and_protocol_extension_values(self):
        for content in ({}, {'items': []}, {'items': [{'variants': [{'item_identity': None}]}]},
                        {'items': [{'metadata': None, 'variants': [{'metadata': None}]}]},
                        {'extension': {'opaque': [1, {'x': 2}]}}):
            result = compact_catalog_content(content)
            self.assertEqual(result, content)
            self.assertIsNot(result, content)


class CatalogProjectionORMTests(GearBuilderTestDataMixin, TestCase):
    def test_catalog_consumers_and_submitted_references_preserve_central_facts(self):
        from botend.services.gear_assistant import _fixed_entries
        self.helm.metadata = deepcopy(RAW_METADATA)
        self.helm.save(update_fields=['metadata'])
        self.hero.metadata = {**RAW_METADATA, 'venomstone': True, 'item_set_id': 1919}
        self.hero.save(update_fields=['metadata'])
        self.enchant.metadata = {**self.enchant.metadata, **RAW_METADATA, 'enchantment_id': 8001}
        self.enchant.save(update_fields=['metadata'])
        stored_items = list(WowItemSnapshot.objects.order_by('pk').values())
        stored_variants = list(WowItemVariantSnapshot.objects.order_by('pk').values())
        content = {'items': gb.catalog_snapshot_items(class_name='Warrior', spec_name='Fury',
                    slot='head', season=self.season),
                   'source_groups': {key: sorted(values) for key, values in gb.QUICK_SOURCE_FILTERS.items()}}
        original = deepcopy(content)
        projected = compact_catalog_content(content)
        self.assertNotIn('difficulty_rule_evidence', projected['items'][0]['variants'][0]['metadata'])
        self.assertEqual(presentation(content['items']), presentation(projected['items']))
        options = [{}, {'source': 'raid'}, {'excludedStats': ['crit']}, {'excludedSources': ['raid']},
                   {'query': str(self.helm.item_id)}, {'query': '锻造'}, {'page': 2, 'pageSize': 1}]
        self.assertEqual(without_private_facts(filtered(content, options)),
                         without_private_facts(filtered(projected, options)))
        enhancements = gb.enhancement_snapshot_payload(class_name='Warrior', spec_name='Fury',
                                                       slot='head', season=self.season)
        public_enhancements = compact_catalog_content(enhancements)
        self.assertEqual(without_private_facts(enhancements), without_private_facts(public_enhancements))
        self.assertEqual(presentation([i for rows in enhancements['groups'].values() for i in rows]),
                         presentation([i for rows in public_enhancements['groups'].values() for i in rows]))
        allowed = gb.filter_enhancement_snapshot(public_enhancements, self.crafted.pk)['embellishments']
        self.assertEqual([r['item_id'] for r in allowed], [self.embellishment_item.item_id])
        crafted = next(i for i in projected['items'] if i['item_id'] == self.crafted_item.item_id)
        v = crafted['variants'][0]
        material = allowed[0]
        entry = {'item': crafted, 'variant': v, 'selectedStats': ['haste', 'mastery'],
                 'embellishment': {'item': material, 'variant': material['variants'][0]}}
        fixed = _fixed_entries({'head': entry}, 'Warrior', 'Fury')['head']
        self.assertEqual(fixed['variant'].pk, self.crafted.pk)
        self.assertEqual(fixed['fixed_enhancements']['embellishment'].pk, self.embellishment.pk)
        resolved = gb.resolve_crafted_variant(variant_id=v['id'], selected_stats=entry['selectedStats'],
                                              embellishment_variant_id=material['variants'][0]['id'])
        self.assertEqual(resolved['resolved_stats']['haste'], 350)
        self.assertEqual(resolved['resolved_stats']['mastery'], 350)
        shared = gb.hydrate_shared_state(share_version=4, class_name='Warrior', spec_name='Fury',
            batch_key=self.season.gear_batch_key, entries=[['head', crafted['item_id'], v['key'],
                entry['selectedStats'], [material['item_id'], material['variants'][0]['key']], [], 0, 0]])
        self.assertEqual(shared['equipment']['head']['variant']['id'], v['id'])
        self.assertEqual(content, original)
        self.assertEqual(list(WowItemSnapshot.objects.order_by('pk').values()), stored_items)
        self.assertEqual(list(WowItemVariantSnapshot.objects.order_by('pk').values()), stored_variants)


class CatalogProjectionCapturedIdentityTests(TestCase):
    def test_captured_exact_identity_and_spec_projection_are_not_lost(self):
        from botend.services.wow_item_identity import identity_reference
        base = deepcopy(identity_fixture.FIXTURE['catalog']['items'][0])
        item = WowItemSnapshot.objects.create(**base)
        rows = identity_fixture.FIXTURE['catalog']['variants']
        season = SeasonMeta.objects.create(id=rows[0]['season_id'], season_key='projection',
            season_name='projection', mplus_zone_id=1, raid_zone_id=2, is_active=True,
            gear_batch_key=rows[0]['batch_key'])
        for row in rows:
            WowItemVariantSnapshot.objects.create(**deepcopy(row))
        fact = identity_fixture.fact(identity_fixture.NEW)
        item.metadata['item_identity_by_build'] = {identity_fixture.NEW: {'is_ptr': True, 'fact': fact}}
        item.save(update_fields=['metadata'])
        before = deepcopy(item.metadata)
        payload = {'items': gb.catalog_snapshot_items(class_name='Mage', spec_name='Fire', slot='legs', season=season)}
        projected = compact_catalog_content(payload)
        self.assertEqual(len(projected['items']), 1)
        self.assertEqual(len(projected['items'][0]['variants']), len(rows))
        expected = identity_reference(fact, is_ptr=True)
        for v in projected['items'][0]['variants']:
            self.assertEqual(v['item_identity'], expected)
            self.assertEqual(v['compatible_slots'], ['legs'])
        self.assertEqual(without_private_facts(projected), without_private_facts(payload))
        self.assertEqual(presentation(payload['items']), presentation(projected['items']))
        self.assertEqual(gb.catalog_snapshot_items(class_name='Warrior', spec_name='Fury', slot='legs', season=season), [])
        item.refresh_from_db()
        self.assertEqual(item.metadata, before)
