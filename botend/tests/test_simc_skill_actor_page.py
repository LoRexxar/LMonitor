"""Bounded read-path contracts; fixtures are central SQL parts, not domain copies."""
import copy
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, SimpleTestCase

from botend.dashboard.api import SimcSkillDamageSnapshotAPIView
from botend.models import SimcSkillDamageSnapshot, SimcSkillDamageSnapshotPart
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Store
from botend.services.simc_skill_actor_page import (
    ActorDocumentParser, ActorPageError, ActorPageService, compact_source_facts,
    attach_page_source_context,
)
from botend.services.simc_skill_source_context import attach_skill_damage_source_context


class ActorParserTests(SimpleTestCase):
    def test_every_character_split_preserves_nested_strings_and_headers(self):
        actor = {'actions': [{'token': 'a', 'nested': ['} , \\"', {'中': '🌟'}]},
                             {'token': 'b', 'value': 1.25}], 'class': '法师'}
        text = json.dumps(actor, ensure_ascii=False)
        parser = ActorDocumentParser(iter(text))
        self.assertEqual(list(parser.actions()), actor['actions'])
        self.assertEqual(parser.header, {'class': '法师'})
        with self.assertRaises(ValueError):
            list(ActorDocumentParser(iter(text + 'garbage')).actions())

    def test_oversize_action_has_fragment_reference_not_silent_drop(self):
        parser = ActorDocumentParser(iter(['{"actions":[{"token":"', 'x'*100, '"}]}']), max_value_chars=40)
        with self.assertRaises(ActorPageError) as error:
            list(parser.actions())
        self.assertEqual(error.exception.details['source_ordinal'], 0)
        self.assertEqual(error.exception.code, 'action_too_large')
        self.assertEqual(error.exception.details['worker_error']['source_ordinal'], 0)

    def test_actor_wide_compact_sources_match_full_resolver_including_label_collisions(self):
        actions = [
            {'token': 'a', 'spell_id': 1, 'display_name': '相同'},
            {'token': 'b', 'spell_id': 2, 'display_name': '相同'},
            {'token': 'childa', 'parent_token': 'a'},
            {'token': 'childb', 'parent_token': 'b'},
            {'token': 'amb', 'spell_id': 3}, {'token': 'amb', 'spell_id': 4},
            {'token': 'childamb', 'parent_token': 'amb'},
            {'token': 'cycle', 'parent_token': 'cycle2'},
            {'token': 'cycle2', 'parent_token': 'cycle'},
        ]
        expected = attach_skill_damage_source_context({'actions': copy.deepcopy(actions)})
        facts = compact_source_facts(iter(actions))
        page = {'actions': copy.deepcopy([actions[2], actions[6], actions[7]])}
        attach_page_source_context(page, facts)
        for row, ordinal in zip(page['actions'], [2, 6, 7]):
            self.assertEqual(row, expected['actions'][ordinal])


class ActorPageAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='actor-page-reader')
        self.snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a'*40, game_build='12.1.5.70575',
            schema_revision=SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION,
            status='succeeded', generated_spec_count=1, generated_action_count=9,
            payload={'payload_format': 'skill_damage_product_v1', 'storage_format': 'per_spec_actor_rows_v1',
                     'wire_schema_revision': 1, 'total_spec_count': 1})
        self.actor = {'class': 'mage', 'specialization': 'fire', 'hero_talent_trees': [],
                      'actions': [{'token': f'action_{i}', 'display_name': f'技能{i}',
                                   'player_skill': True, 'product': {'supported': True,
                                   'final_normalized_damage': i + 1}, 'description': '事实'*20000}
                                  for i in range(9)]}
        self.row = Store.create_actor(snapshot=self.snapshot, ordinal=0, class_name='mage',
            specialization='fire', actor_payload=self.actor, unresolved_payload=[{'reason': 'known'}],
            raw_action_count=9, display_action_count=9)

    def request(self, **params):
        request = RequestFactory().get('/api/simc-skill-damage/', {
            'view': 'actor_page', 'snapshot_id': self.snapshot.pk, 'actor_id': self.row.pk,
            'limit': 2, **params})
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        try:
            data = json.loads(b''.join(response.streaming_content) if response.streaming else response.content)
        finally:
            response.close()
        return response.status_code, data

    def test_real_node_sql_parts_paging_cursor_binding_and_immutability(self):
        with mock.patch.object(Store, 'read_document', side_effect=AssertionError('full actor forbidden')):
            status, data = self.request()
            self.assertEqual(status, 200, data)
            snapshot = data['data']['snapshot']
            page = snapshot['actor_page']
            self.assertEqual(page['source_ordinals'], [8, 7])
            self.assertEqual(page['counts']['stored_actions'], 9)
            self.assertEqual(page['counts']['matched'], 9)
            self.assertEqual(snapshot['actors'][0]['actions'][0], self.actor['actions'][8])
            token = page['page']['next_cursor']
            self.assertTrue(token)
            status, second = self.request(cursor=token)
            self.assertEqual(status, 200, second)
            self.assertEqual(second['data']['snapshot']['actor_page']['source_ordinals'], [6, 5])
            self.assertEqual(self.request(cursor=token, q='changed')[0], 400)
            self.assertEqual(self.request(cursor=token + 'x')[0], 400)
        self.row.refresh_from_db()
        self.assertNotIn('actions', self.row.actor_payload)
        self.assertEqual(Store.read_document(self.row, 'actor_payload'), self.actor)

    def replace_actor(self, actor, unresolved=None):
        self.row.delete()
        self.actor = actor
        self.row = Store.create_actor(snapshot=self.snapshot, ordinal=0, class_name='mage',
            specialization='fire', actor_payload=actor, unresolved_payload=unresolved or [],
            raw_action_count=len(actor['actions']), display_action_count=len(actor['actions']))

    def test_large_diagnostic_footer_is_referenced_without_blocking_page_or_action(self):
        diagnostic = {'evidence': '诊断\\"🌟' * 430000}
        actor = {'actions': [{'token': 'small', 'product': {'final_normalized_damage': 1}},
                             {'token': 'higher', 'product': {'final_normalized_damage': 2}}],
                 'hero_talent_trees': [{'id': 11, 'name': 'Hero'}],
                 'global_skill_effects': [], 'reviewed_global_effects': [],
                 'specialization_passive_diagnostics': diagnostic}
        self.replace_actor(actor)
        raw = json.dumps(diagnostic, ensure_ascii=False, separators=(',', ':'))
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        real_loads = json.loads
        def bounded_loads(text, *args, **kwargs):
            self.assertLessEqual(len(text), 2 * 1024 * 1024)
            return real_loads(text, *args, **kwargs)
        with mock.patch.object(Store, 'read_document', side_effect=AssertionError('full read forbidden')), \
             mock.patch('botend.services.simc_skill_actor_page.json.loads', side_effect=bounded_loads):
            status, data = self.request(limit=1)
            self.assertEqual(status, 200, data)
            snapshot = data['data']['snapshot']
            self.assertNotIn('specialization_passive_diagnostics', snapshot['actors'][0])
            self.assertIn('global_skill_effects', snapshot['actors'][0])
            self.assertIn('reviewed_global_effects', snapshot['actors'][0])
            self.assertEqual(snapshot['actors'][0]['hero_talent_trees'], actor['hero_talent_trees'])
            self.assertEqual(snapshot['actor_page']['source_ordinals'], [1])
            ref, = snapshot['actor_page']['header_refs']
            self.assertEqual(ref['field'], 'specialization_passive_diagnostics')
            self.assertEqual(ref['total_characters'], len(raw))
            self.assertEqual(ref['document_sha256'], self.row.payload_storage['documents']['actor_payload']['sha256'])
            self.assertEqual(ref['snapshot_id'], self.snapshot.pk)
            self.assertEqual(ref['actor_id'], self.row.pk)
            self.assertEqual(ref['view'], 'actor_header_fragment')
            status, second = self.request(limit=1, cursor=snapshot['actor_page']['page']['next_cursor'])
            self.assertEqual(status, 200, second)
            self.assertEqual(second['data']['snapshot']['actor_page']['source_ordinals'], [0])
            self.assertFalse(second['data']['snapshot']['actor_page']['page']['has_more'])
            self.assertEqual(second['data']['snapshot']['actor_page']['header_refs'], [ref])
            status, action = self.request(view='actor_fragment', source_ordinal=0, fragment_limit=16)
            self.assertEqual(status, 200, action)
            self.assertEqual(action['data']['fragment']['text'],
                json.dumps(actor['actions'][0], ensure_ascii=False, separators=(',', ':'))[:16])
            # Cross a real central-part boundary within the original JSON value.
            prefix = json.dumps(actor, ensure_ascii=False, separators=(',', ':')).index(raw)
            offset = Store.PART_CHARACTERS - prefix - 9
            pieces = []
            for start in (offset, offset + 16):
                status, detail = self.request(view=ref['view'], field=ref['field'],
                    offset=start, fragment_limit=16)
                self.assertEqual(status, 200, detail)
                fragment = detail['data']['fragment']
                self.assertEqual(fragment['field'], ref['field'])
                self.assertEqual(fragment['document_sha256'], ref['document_sha256'])
                self.assertEqual(fragment['total_characters'], len(raw))
                self.assertEqual(fragment['next_offset'], start + 16)
                pieces.append(fragment['text'])
            self.assertEqual(''.join(pieces), raw[offset:offset + 32])
        self.assertEqual(Store.read_document(self.row, 'actor_payload'), actor)

    def test_header_fragments_reconstruct_exact_raw_value_and_validate_query(self):
        value = {'原文': ['🌟\\"\\n' * 4000, {'tail': True}]}
        self.replace_actor({'actions': [], 'specialization_passive_diagnostics': value})
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        offset, pieces = 0, []
        while offset is not None:
            status, data = self.request(view='actor_header_fragment',
                field='specialization_passive_diagnostics', offset=offset, fragment_limit=16384)
            self.assertEqual(status, 200, data)
            fragment = data['data']['fragment']
            self.assertLessEqual(len(fragment['text']), 16384)
            self.assertEqual(fragment['offset'], offset)
            self.assertEqual(fragment['offset_unit'], 'unicode_characters')
            pieces.append(fragment['text'])
            offset = fragment['next_offset']
        self.assertEqual(''.join(pieces), raw)
        for params in ({}, {'field': ''}, {'field': 'actions'},
                       {'field': 'specialization_passive_diagnostics', 'fragment_limit': 16385},
                       {'field': 'specialization_passive_diagnostics', 'offset': -1}):
            self.assertEqual(self.request(view='actor_header_fragment', **params)[0], 400)
        self.assertEqual(self.request(view='actor_header_fragment', field='missing')[0], 404)

    def test_large_diagnostic_footer_corruption_rejects_every_read_view(self):
        self.replace_actor({'actions': [{'token': 'small'}],
            'specialization_passive_diagnostics': {'evidence': 'x' * (2 * 1024 * 1024 + 1)}})
        last = SimcSkillDamageSnapshotPart.objects.filter(actor=self.row,
            document='actor_payload').order_by('-ordinal').first()
        last.text = last.text.replace('x', 'y', 1)
        last.save(update_fields=['text'])
        for params in ({}, {'view': 'actor_fragment', 'source_ordinal': 0},
                       {'view': 'actor_header_fragment', 'field': 'specialization_passive_diagnostics'}):
            status, data = self.request(**params)
            self.assertEqual(status, 503, data)
            self.assertFalse(data['success'])

    def test_header_fragment_rejects_membership_change_after_full_scan(self):
        original = Store.iterate_json_document
        def change_after_read(row, document):
            yield from original(row, document)
            type(self.row).objects.filter(pk=self.row.pk).update(
                payload_storage={})
        with mock.patch.object(Store, 'iterate_json_document', side_effect=change_after_read):
            status, data = self.request(view='actor_header_fragment', field='class', fragment_limit=2)
        self.assertEqual(status, 503, data)
        self.assertEqual(data['code'], 'snapshot_changed')

    def test_required_display_header_still_fails_explicitly_when_over_budget(self):
        for field in ('global_skill_effects', 'reviewed_global_effects'):
            self.replace_actor({'actions': [], field: [{'description': 'x' * (2 * 1024 * 1024)}]})
            status, data = self.request()
            self.assertEqual(status, 413, data)
            self.assertEqual(data['code'], 'header_too_large')
            # A header detail does not need any full display header to fit.
            status, detail = self.request(view='actor_header_fragment', field=field,
                offset=0, fragment_limit=16)
            self.assertEqual(status, 200, detail)
            self.assertEqual(detail['data']['fragment']['text'],
                json.dumps(self.actor[field], separators=(',', ':'))[:16])

    def test_small_byte_page_continues_without_second_document_scan(self):
        with mock.patch.object(Store, 'iterate_json_document', wraps=Store.iterate_json_document) as streamed:
            status, data = self.request(limit=50)
        self.assertEqual(status, 200, data)
        page = data['data']['snapshot']['actor_page']
        self.assertLess(page['page']['returned'], 50)
        self.assertTrue(page['page']['has_more'])
        self.assertTrue(page['page']['next_cursor'])
        self.assertEqual(streamed.call_count, 1)
        self.assertEqual(streamed.call_args.args[1], 'actor_payload')

    def test_localized_global_search_and_full_cohort_facets_precede_exclusion(self):
        from botend.models import WowSpellSnapshot
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=77535,
            snapshot_build=self.snapshot.game_build, name='Blood Shield', name_zh='鲜血护盾',
            aura_description='吸收物理伤害。')
        actor = {'class': 'mage', 'specialization': 'fire', 'actions': [
            {'token': 'cond', 'display_name': '条件技能', 'product': {'final_normalized_damage': 1},
             'variant': {'runtime_conditions': [{'token': 'buff.blood_shield', 'spell_id': 77535,
                                                 'display_name': 'blood_shield', 'scope': 'self'}]}},
            {'token': 'plain', 'display_name': '普通技能', 'product': {'final_normalized_damage': 99}},
            {'token': 'other_hero', 'hero_subtree_ids': [22], 'variant': {'talent_id': 8}},
        ]}
        self.replace_actor(actor)
        status, data = self.request(q='鲜血护盾', hero_tree_id=11)
        self.assertEqual(status, 200, data)
        snapshot = data['data']['snapshot']
        self.assertEqual(snapshot['actor_page']['source_ordinals'], [0])
        self.assertEqual(snapshot['actor_page']['counts']['scope_candidates'], 2)
        self.assertEqual(snapshot['actor_page']['counts']['matched'], 1)
        buff = snapshot['actor_page']['facets']['buffs'][0]
        self.assertIn('鲜血护盾', buff['label'])
        self.assertEqual(snapshot['actor_page']['facets']['talents'], [])
        self.assertEqual(snapshot['actors'][0]['actions'][0]['variant']['runtime_conditions'][0]['display_name'], '鲜血护盾')
        status, excluded = self.request(hero_tree_id=11, exclude=json.dumps([buff['key']]))
        self.assertEqual(status, 200, excluded)
        excluded = excluded['data']['snapshot']['actor_page']
        self.assertEqual(excluded['source_ordinals'], [1])
        self.assertEqual(excluded['facets']['buffs'], [buff])
        self.row.refresh_from_db()
        self.assertEqual(Store.read_document(self.row, 'actor_payload'), actor)

    def test_single_over_budget_action_is_explicit_and_has_bounded_detail(self):
        actor = {'actions': [{'token': 'huge', 'description': 'x' * 300000,
                             'product': {'final_normalized_damage': 100}}]}
        self.replace_actor(actor)
        status, data = self.request()
        self.assertEqual(status, 413, data)
        self.assertEqual(data['code'], 'action_too_large')
        self.assertEqual(data['source_ordinal'], 0)
        self.assertEqual(data['fragment_view'], 'actor_fragment')
        self.assertEqual(data['worker_error']['code'], 'row_exceeds_page_budget')
        self.assertEqual(data['worker_error']['source_ordinal'], 0)
        status, detail = self.request(view='actor_fragment', source_ordinal=0, offset=1000, fragment_limit=16)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail['data']['fragment']['text'], 'x' * 16)
        self.assertEqual(detail['data']['fragment']['next_offset'], 1016)

    def test_inline_cursor_works_and_unresolved_is_bounded_raw_fragment(self):
        self.replace_actor({'actions': [{'token': str(i), 'product': {'final_normalized_damage': i}}
                                       for i in range(6)]}, [{'reason': '未知来源'}])
        status, first = self.request()
        self.assertEqual(status, 200, first)
        token = first['data']['snapshot']['actor_page']['page']['next_cursor']
        status, second = self.request(cursor=token)
        self.assertEqual(status, 200, second)
        self.assertEqual(second['data']['snapshot']['actor_page']['source_ordinals'], [3, 2])
        self.assertEqual(self.request(cursor=token, target_count=2)[0], 400)
        status, unresolved = self.request(view='unresolved_fragment', offset=0, fragment_limit=5)
        self.assertEqual(status, 200, unresolved)
        detail = unresolved['data']['fragment']
        self.assertEqual(detail['document'], 'unresolved_payload')
        self.assertEqual(detail['text'], '[{"re')
        self.assertEqual(detail['next_offset'], 5)

    def test_cursor_rejects_changed_code_presentation_and_inline_dataset(self):
        self.replace_actor({'actions': [{'token': str(i), 'product': {'final_normalized_damage': i}}
                                       for i in range(6)]})
        status, data = self.request()
        self.assertEqual(status, 200, data)
        token = data['data']['snapshot']['actor_page']['page']['next_cursor']
        with mock.patch.object(ActorPageService, '_query_code_version', return_value='new-code'):
            self.assertEqual(self.request(cursor=token)[0], 400)
        with mock.patch('botend.services.simc_skill_actor_page.PRESENTATION_VERSION', 'new-presentation'):
            self.assertEqual(self.request(cursor=token)[0], 400)
        self.row.actor_payload['actions'][0]['token'] = 'dataset-changed'
        self.row.save(update_fields=['actor_payload'])
        self.assertEqual(self.request(cursor=token)[0], 400)

    def test_unresolved_inline_change_during_read_is_rejected(self):
        original = Store.iterate_json_document
        def change_after_read(row, document):
            yield from original(row, document)
            if document == 'unresolved_payload':
                type(self.row).objects.filter(pk=self.row.pk).update(unresolved_payload=[{'reason': 'changed'}])
        with mock.patch.object(Store, 'iterate_json_document', side_effect=change_after_read):
            status, data = self.request(view='unresolved_fragment', fragment_limit=5)
        self.assertEqual(status, 503, data)
        self.assertEqual(data['code'], 'snapshot_changed')

    def test_missing_last_part_and_unresolved_checksum_never_return_fragment_success(self):
        last = SimcSkillDamageSnapshotPart.objects.filter(actor=self.row).order_by('-ordinal').first()
        last.delete()
        self.assertEqual(self.request()[0], 503)
        self.replace_actor({'actions': []}, [{'reason': 'x' * 300000}])
        last = SimcSkillDamageSnapshotPart.objects.filter(actor=self.row,
            document='unresolved_payload').order_by('-ordinal').first()
        last.text = last.text.replace('x', 'y', 1)
        last.save(update_fields=['text'])
        status, data = self.request(view='unresolved_fragment', fragment_limit=5)
        self.assertEqual(status, 503, data)
        self.assertFalse(data['success'])

    def test_corrupt_eof_checksum_never_returns_success(self):
        last = SimcSkillDamageSnapshotPart.objects.filter(actor=self.row).order_by('-ordinal').first()
        last.text = last.text.replace('事实', '失实', 1)
        last.save(update_fields=['text'])
        status, data = self.request()
        self.assertEqual(status, 503, data)
        self.assertFalse(data['success'])

    def test_legacy_oversize_guard_happens_before_materialization(self):
        with mock.patch.object(SimcSkillDamageSnapshotAPIView, '_RESPONSE_SPOOL_MAX_BYTES', 100), \
             mock.patch.object(Store, 'read_document', side_effect=AssertionError('guard too late')):
            response = SimcSkillDamageSnapshotAPIView._sharded_snapshot_response(
                self.snapshot, job=None, can_generate=False)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.content)['paged_view']['view'], 'actor_page')

    def test_bounded_fragment_path_and_explicit_unresolved_reference(self):
        status, data = self.request(view='actor_fragment', source_ordinal=2, offset=4, fragment_limit=23)
        self.assertEqual(status, 200, data)
        fragment = data['data']['fragment']
        original = json.dumps(self.actor['actions'][2], ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(fragment['text'], original[4:27])
        self.assertEqual(fragment['next_offset'], 27)
        status, data = self.request()
        self.assertEqual(status, 200, data)
        unresolved = data['data']['snapshot']['actor_page']['unresolved']
        self.assertEqual(unresolved['availability'], 'separate_document')
        self.assertNotIn('count', unresolved)
