"""职业攻略完整流程与权限、修订、引用保真回归。"""

import copy
import json
import hashlib
import uuid
from pathlib import Path
from django.core.management import call_command
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings, SimpleTestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.management.base import CommandError
from botend.constants.wow import SPEC_IDENTITY_MAP, resolve_spec_identity
from botend.services.class_guide_authors import extract_author_profile
from botend.services.class_guide_monitor import get_guide_monitor_task
from botend.models import MonitorTaskLease, MonitorTaskLeaseLost
from botend.plugin_sync import claim_monitor_task
from bs4 import BeautifulSoup

from botend.guide_models import ClassGuide, ClassGuideTranslation
from botend.services.wow_localization import write_name, effective_names
from botend.models import WowTalentNodeMetadata, WowSpellSnapshot
from botend.services.class_guide_codec import decode_component, snappy
from botend.services.class_guide_content import clean_html, validate_blocks, render_references, resolve_references
from botend.services.class_guide_maxroll import discover, convert
from botend.services.class_guide_markdown import compile_markdown, blocks_to_markdown, html_to_markdown, normalize_tab_markdown
from botend.services.wow_news_glossary_service import WowNewsGlossary
from botend.services.class_guide_service import protect_guide_text, import_post, save_article, check_article, ArticleConflict, sync_guides, translate_blocks


def create_name(**fields):
    return write_name(fields)


def source_post():
    return {'slug': 'arcane-mage-raid-guide', 'title': 'Arcane Mage Raid Guide',
        'tags': [{'name': '12.1 - Midnight'}], 'modifiedIso': '2026-09-07', 'author': {'name': '作者'},
        'gutenbergBlock': [{'blockName': 'maxroll/title-separator', 'attributes': {'title': 'Overview'}},
            {'blockName': 'core/paragraph', 'innerHTML': '<p>Cast <span class="wow-spell" data-wow-id="30451">Arcane Blast</span>.</p>'}]}


@override_settings(SECURE_SSL_REDIRECT=False)
class GuideFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username='guide_editor', password='test-only')
        self.client.force_login(self.user)
        self.url = 'https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide'
        self.guide, _ = import_post(source_post(), self.url)

    def test_import_is_idempotent_and_version_isolated(self):
        _, state = import_post(source_post(), self.url)
        self.assertEqual(state, 'unchanged')
        post = source_post(); post['tags'] = [{'name': '12.2'}]
        other, _ = import_post(post, self.url)
        self.assertNotEqual(other.id, self.guide.id)

    def test_management_pages_share_existing_editor_permission(self):
        from botend.models import DashboardUserGroup
        editor = get_user_model().objects.create_user(username='术语编辑', is_staff=True)
        self.client.force_login(editor)
        urls = ['/dashboard/?section=guide-disclaimers', '/dashboard/?section=wow-localization']
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        group = DashboardUserGroup.objects.create(name='攻略编辑权限', permission_codes=['content.class-guides', 'tools.wow-localization'])
        group.users.add(editor)
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            document = BeautifulSoup(response.content, 'html.parser')
            self.assertIsNotNone(document.select_one('#guide-disclaimers #guide-disclaimer-form'))
            self.assertIsNotNone(document.select_one('#wow-localization #wow-localization-search'))
            self.assertIsNone(document.select_one('#class-guide-workspace #disclaimer-dialog'))
            self.assertIsNone(document.select_one('#class-guide-workspace #terms-browser'))

    def test_term_management_lists_versions_and_preserves_version_filter(self):
        for version, name in [('12.1', '旧版名称'), ('12.2', '新版名称')]:
            create_name(game_version=version, kind='spell', object_id=30451,
                name_en='Arcane Blast', name_zh=name, evidence='版本对照')
        endpoint = '/api/dashboard/wow-localization/'
        data = self.client.get(endpoint, {'q': '30451', 'kind': 'spell'}).json()
        self.assertEqual(data['total'], 2)
        self.assertTrue({'12.1', '12.2'}.issubset(data['versions']))
        filtered = self.client.get(endpoint, {'version': '12.2', 'q': '30451'}).json()
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['records'][0]['name_zh'], '新版名称')
        self.assertEqual(Client().get(endpoint).status_code, 403)

    def test_refresh_translations_rebuilds_unchanged_source_as_candidate(self):
        with patch('botend.services.class_guide_service.translate_blocks', return_value=([{'id':'p','type':'html','html':'<p>第一版中文</p>'}], [])):
            import_post(source_post(), self.url, translate=True)
        with patch('botend.services.class_guide_service.translate_blocks', return_value=([{'id':'p','type':'html','html':'<p>术语修正后中文</p>'}], [])) as translate:
            _, status = import_post(source_post(), self.url, translate=True)
            self.assertEqual(status, 'unchanged')
            translate.assert_not_called()
            import_post(source_post(), self.url, translate=True, refresh_translations=True)
            _, status = import_post(source_post(), self.url, translate=True, refresh_translations=True)
            self.assertEqual(status, 'unchanged')
        self.assertIn('术语修正后中文', ClassGuide.objects.get(pk=self.guide.pk).content_markdown)

    def test_macro_name_override_does_not_replace_boss_reference(self):
        create_name(game_version='12.1', kind='spell', object_id=1300877, name_en='Corruption', name_zh='腐化')
        response = self.client.post('/api/dashboard/wow-localization/', data=json.dumps({'game_version':'12.1', 'kind':'macro', 'name_en':'Corruption', 'name_zh':'腐蚀术', 'evidence':'玩家技能 ID 172'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        service = Mock(); service.available.return_value = False
        result, failed = translate_blocks([{'id':'macro','type':'code','data':{'code':'/cast Corruption'}}, {'id':'boss','type':'html','html':'<p>[[spell:1300877]]</p>'}], '12.1', service=service)
        self.assertFalse(failed)
        self.assertEqual(result[0]['data']['code'], '/cast 腐蚀术')
        self.assertEqual(resolve_references(result, '12.1')['[[spell:1300877]]']['name'], '腐化')

    def test_manual_content_survives_source_update_and_default_editor(self):
        self.guide.refresh_from_db()
        manual = save_article(self.guide.id, '人工中文攻略', [{'id': 'intro', 'type': 'html', 'html': '<p>人工内容</p>'}], expected_updated_at=ClassGuide.objects.get(pk=self.guide.pk).updated_at)
        post = source_post(); post['gutenbergBlock'][1]['innerHTML'] += '<p>Updated paragraph.</p>'
        import_post(post, self.url)
        manual.refresh_from_db()
        self.assertIn('人工内容', manual.blocks[0]['html'])
        self.guide.refresh_from_db()
        self.assertEqual(self.guide.title, '人工中文攻略')
        data = self.client.get(f'/api/dashboard/class-guides/{self.guide.id}/').json()
        self.assertEqual(data['id'], manual.id)

    def test_exported_drafts_can_be_imported_without_retranslation(self):
        folder = Path('tmp/django-tests/class-guides-' + uuid.uuid4().hex)
        folder.mkdir(parents=True, exist_ok=True)
        call_command('audit_class_guides', output_dir=str(folder), game_version='12.1', verbosity=0)
        slug = self.guide.slug
        self.guide.slug = 'original-' + slug
        self.guide.save(update_fields=['slug'])
        call_command('import_class_guide_drafts', input_dir=str(folder), dry_run=True, verbosity=0)
        self.assertEqual(ClassGuide.objects.count(), 1)
        with patch('botend.services.class_guide_service.build_translation_service') as engine:
            call_command('import_class_guide_drafts', input_dir=str(folder), verbosity=0)
            call_command('import_class_guide_drafts', input_dir=str(folder), verbosity=0)
            engine.assert_not_called()
        imported = ClassGuide.objects.get(slug=slug)
        self.assertEqual(ClassGuide.objects.get(pk=imported.pk).content_markdown, ClassGuide.objects.get(pk=self.guide.pk).content_markdown)
        manual = save_article(imported.id, '人工稿', content_markdown='人工保留的正文', expected_updated_at=imported.updated_at)
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        draft_path = folder / (slug + '-12.1.json')
        draft = json.loads(draft_path.read_text(encoding='utf-8'))
        draft['content_markdown'] += '\n新增来源段落。'
        manifest['records'][0]['markdown_sha256'] = hashlib.sha256(draft['content_markdown'].encode()).hexdigest()
        draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        call_command('import_class_guide_drafts', input_dir=str(folder), verbosity=0)
        current = self.client.get(f'/api/dashboard/class-guides/{imported.id}/').json()
        self.assertEqual(current['id'], manual.id)

    def test_stale_editor_cannot_overwrite(self):
        with self.assertRaises(ArticleConflict):
            save_article(self.guide.id, '过期内容', [], expected_updated_at=timezone.now() - timedelta(days=1))

    def test_manual_empty_content_is_not_restored_by_monitor(self):
        guide = ClassGuide.objects.get(pk=self.guide.pk)
        save_article(guide.id, '人工清空正文', content_markdown='', expected_updated_at=guide.updated_at)
        with patch('botend.services.class_guide_service.translate_blocks') as translate:
            updated, status = import_post(source_post(), self.url, translate=True)
        self.assertEqual(status, 'manual_preserved')
        self.assertEqual(updated.content_markdown, '')
        translate.assert_not_called()

    def test_source_translation_cannot_overwrite_edit_saved_during_translation(self):
        def translate(*args, **kwargs):
            guide = ClassGuide.objects.get(pk=self.guide.pk)
            save_article(guide.pk, '同时人工编辑', content_markdown='人工正文', expected_updated_at=guide.updated_at)
            return ([{'id': 'p', 'type': 'html', 'html': '<p>自动译文</p>'}], [])
        with patch('botend.services.class_guide_service.translate_blocks', side_effect=translate):
            with self.assertRaises(ArticleConflict):
                import_post(source_post(), self.url, translate=True)
        self.assertEqual(ClassGuide.objects.get(pk=self.guide.pk).content_markdown, '人工正文')

    def test_historical_terms_require_matching_identity_and_preserve_manual_edits(self):
        folder = Path('tmp/django-tests/guide-terms-' + uuid.uuid4().hex)
        current, old = folder / 'current', folder / 'history'
        current.mkdir(parents=True); old.mkdir()
        for locale, name in [('enUS', 'Arcane Blast'), ('zhCN', '奥术冲击')]:
            (current / ('SpellName_' + locale + '.csv')).write_text('ID,Name_lang\n', encoding='utf-8')
            (old / ('SpellName_' + locale + '.csv')).write_text('ID,Name_lang\n30451,' + name + '\n', encoding='utf-8')
        options = {'game_version':'12.1', 'snapshot_build':'12.1.0.1', 'spell_names_dir':str(current),
            'historical_snapshot':['11.2.7.1=' + str(old)], 'verbosity':0}
        revision = ClassGuide.objects.get(pk=self.guide.pk)
        revision.check_data['source_refs']['[[spell:30451]]']['source_name'] = 'Fireball'
        revision.save(update_fields=['check_data'])
        call_command('hydrate_class_guide_terms', **options)
        self.assertFalse(effective_names('12.1'))
        revision.check_data['source_refs']['[[spell:30451]]']['source_name'] = 'Arcane Blast'
        revision.save(update_fields=['check_data'])
        call_command('hydrate_class_guide_terms', **options)
        term = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=30451)
        self.assertEqual(term.name_zh, '奥术冲击')
        self.assertIn('11.2.7.1', term.localization_evidence)
        self.assertEqual(check_article(revision)['historical_references'], ['[[spell:30451]]'])
        term.name_zh = '人工核对的名称'; term.localization_evidence = '人工核对'; term.save()
        call_command('hydrate_class_guide_terms', **options)
        term.refresh_from_db()
        self.assertEqual(term.name_zh, '人工核对的名称')

    def test_reference_label_conflicts_are_visible_for_review(self):
        create_name(game_version='12.1', kind='spell', object_id=30451,
            name_en='Fireball', name_zh='火球术', evidence='测试名称冲突')
        audit = check_article(ClassGuide.objects.get(pk=self.guide.pk))
        self.assertEqual(audit['source_name_mismatches'], ['[[spell:30451]]'])
        self.assertEqual(audit['references']['[[spell:30451]]']['name_en'], 'Fireball')

    def test_phrase_terms_are_editable_and_protected_during_translation(self):
        data = {'game_version':'12.1','kind':'phrase','name_en':"Blood of Ula'tek", 'name_zh':'乌拉特克之血','evidence':'冒险指南中英对照'}
        response = self.client.post('/api/dashboard/wow-localization/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        repeated = self.client.post('/api/dashboard/wow-localization/', json.dumps(data), content_type='application/json')
        self.assertEqual(repeated.json()['id'], response.json()['id'])
        record = WowTalentNodeMetadata.all_objects.get(pk=response.json()['id'].split(':')[1])
        self.assertLess(record.reference_id, 2**53)
        service = Mock(); service.available.return_value = False
        translated, failed = translate_blocks([{'id':'boss','type':'html','html':"<p>Blood of Ula'tek</p>"}], '12.1', service=service)
        self.assertFalse(failed)
        self.assertIn('乌拉特克之血', translated[0]['html'])
        service.engine.send_message.assert_not_called()
        for i in range(105):
            create_name(game_version='12.1',kind='item',object_id=10000+i,
                name_en='Item '+str(i),name_zh='测试装备 '+str(i),evidence='分页测试')
        second = self.client.get('/api/dashboard/wow-localization/?version=12.1&page=2').json()
        self.assertEqual(second['total'], 106)
        self.assertEqual(len(second['records']), 6)
        search = self.client.get('/api/dashboard/wow-localization/', {'version':'12.1','q':'乌拉特克','kind':'phrase'}).json()
        self.assertEqual(search['total'], 1)

    def test_source_failure_is_recorded_and_releases_lease(self):
        task = get_guide_monitor_task(); task.notes = '测试授权'; task.save(update_fields=['notes'])
        with patch('botend.services.class_guide_service.MaxrollClient') as cls:
            cls.return_value.discover.return_value = [self.url]
            cls.return_value.article.side_effect = ValueError('模拟断网')
            run = sync_guides()
        self.assertEqual(run.status, 'partial')
        self.assertEqual(run.results[0]['status'], 'failed')
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def test_active_lease_prevents_second_worker(self):
        task = get_guide_monitor_task(); task.notes = '测试授权'; task.save(update_fields=['notes'])
        claim_monitor_task(task.pk)
        with self.assertRaises(MonitorTaskLeaseLost):
            sync_guides()

    def test_authorization_is_required_before_fetch(self):
        with patch('botend.services.class_guide_service.MaxrollClient') as client:
            with self.assertRaises(ValueError):
                sync_guides()
            client.assert_not_called()

    def test_permissions_and_no_public_route(self):
        anonymous = Client()
        for url in ['/dashboard/class-guides/', '/api/dashboard/class-guides/',
                    f'/dashboard/class-guides/{self.guide.id}/preview/']:
            self.assertEqual(anonymous.get(url).status_code, 403, url)
        self.assertEqual(anonymous.get('/portal/class-guides/').status_code, 403)
        plain = get_user_model().objects.create_user(username='guide_reader')
        anonymous.force_login(plain)
        self.assertEqual(anonymous.get('/api/dashboard/class-guides/').status_code, 403)

    def test_csrf_required_for_mutation(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.user)
        self.assertEqual(client.post('/api/dashboard/class-guides/', data='{}', content_type='application/json').status_code, 403)

    def test_dynamic_tags_without_required_version_or_type(self):
        data = {'title':'奥术新手指南','slug':'arcane-beginner','class_name':'mage','spec_name':'arcane',
            'tags':['新手向',' 自定义主题 ','新手向'], 'content_markdown':'## 概览\n\n中文正文。'}
        response = self.client.post('/api/dashboard/class-guides/', data=json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        guide = ClassGuide.objects.get(pk=response.json()['id'])
        self.assertEqual(set(guide.tags.values_list('name',flat=True)), {'新手向','自定义主题'})
        catalog = self.client.get('/api/dashboard/class-guides/', {'tag':'自定义主题'}).json()
        self.assertEqual([row['id'] for row in catalog['records']], [guide.id])
        self.assertIn('新手向', catalog['tags'])
        data['slug']='invalid-tag';data['tags']=['x'*61]
        self.assertEqual(self.client.post('/api/dashboard/class-guides/',data=json.dumps(data),content_type='application/json').status_code,400)
        self.assertFalse(ClassGuide.objects.filter(slug='invalid-tag').exists())

    def test_specialization_required_and_canonical_identity(self):
        endpoint = '/api/dashboard/class-guides/'
        body = {'title': '神圣攻略', 'slug': 'holy-new', 'content_markdown': '## 概览\n中文正文。'}
        for identity in ({}, {'spec_id': ''}, {'spec_id': True}, {'spec_id': 999},
                         {'spec_id': 65, 'class_name': 'Priest', 'spec_name': 'Holy'}):
            response = self.client.post(endpoint, data=json.dumps({**body, **identity}), content_type='application/json')
            self.assertEqual(response.status_code, 400, response.content)
        response = self.client.post(endpoint, data=json.dumps({**body, 'spec_id': '65'}), content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        guide = ClassGuide.objects.get(pk=response.json()['id'])
        self.assertEqual((guide.spec_id, guide.class_name, guide.spec_name), (65, 'Paladin', 'Holy'))
        self.assertEqual((self.guide.spec_id, self.guide.class_name, self.guide.spec_name), (62, 'Mage', 'Arcane'))
        self.assertEqual(resolve_spec_identity(None, 'hunter', 'beast_mastery'), (253, 'Hunter', 'BeastMastery'))

    def test_same_name_specializations_have_distinct_filters(self):
        for spec_id in (65, 257):
            ClassGuide.objects.create(title='神圣攻略', slug=f'holy-{spec_id}', game_version='current', spec_id=spec_id)
        catalog = self.client.get('/api/dashboard/class-guides/', {'spec_id': 257}).json()
        self.assertEqual([row['spec_id'] for row in catalog['records']], [257])
        self.assertEqual(catalog['records'][0]['specialization_label'], '牧师 · 神圣')
        self.assertEqual({row['spec_id'] for row in catalog['specializations']}, set(SPEC_IDENTITY_MAP))
        self.assertEqual(next(row['spec_label'] for row in catalog['specializations'] if row['spec_id'] == 253), '野兽控制')

    def test_database_rejects_specialization_mismatch_and_missing_binding(self):
        with self.assertRaises(ValidationError):
            ClassGuide.objects.create(title='无效', slug='invalid', game_version='current')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassGuide.objects.filter(pk=self.guide.pk).update(spec_id=257)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassGuide.objects.bulk_create([ClassGuide(title='无效', slug='invalid', game_version='current', spec_id=62, class_name='Priest', spec_name='Holy')])

    def test_source_preserves_manually_selected_specialization(self):
        response = self.client.patch(f'/api/dashboard/class-guides/{self.guide.pk}/',
            data=json.dumps({'expected_updated_at':ClassGuide.objects.get(pk=self.guide.pk).updated_at.isoformat(), 'spec_id': 257}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        ClassGuide.objects.filter(pk=self.guide.pk).update(spec_id=257, class_name='Priest', spec_name='Holy')
        guide, status = import_post(source_post(), self.url)
        self.assertEqual(status, 'specialization_preserved')
        self.assertEqual(guide.spec_id, 257)

    def test_visibility_and_specialization_save_with_content(self):
        endpoint = f'/api/dashboard/class-guides/{self.guide.pk}/'
        current = self.client.get(endpoint).json()
        self.assertEqual({s['spec_id'] for s in current['specializations']}, set(SPEC_IDENTITY_MAP))
        body = {'expected_updated_at': current['updated_at'], 'title': '调整专精后的文章',
            'content_markdown': '## 正文\n同时保存的内容', 'spec_id': 257, 'is_visible': False}
        response = self.client.patch(endpoint, json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        guide = ClassGuide.objects.get(pk=self.guide.pk)
        self.assertEqual((guide.spec_id, guide.class_name, guide.spec_name), (257, 'Priest', 'Holy'))
        self.assertTrue(guide.archived)
        self.assertIn('同时保存的内容', guide.content_markdown)
        self.assertEqual(self.client.get(f'/portal/class-guides/{guide.pk}/').status_code, 404)
        self.assertNotContains(self.client.get('/portal/class-guides/'), guide.title)
        self.assertEqual(self.client.get('/api/dashboard/class-guides/').json()['total'], 1)
        self.assertEqual(self.client.get('/api/dashboard/class-guides/?visible=0').json()['total'], 1)
        self.assertEqual(self.client.get('/api/dashboard/class-guides/?visible=1').json()['total'], 0)
        self.assertEqual(self.client.patch(endpoint, json.dumps(body), content_type='application/json').status_code, 409)
        body.update(expected_updated_at=self.client.get(endpoint).json()['updated_at'], is_visible=True)
        self.assertEqual(self.client.patch(endpoint, json.dumps(body), content_type='application/json').status_code, 200)
        self.assertContains(self.client.get(f'/portal/class-guides/{guide.pk}/'), '同时保存的内容')

    def test_invalid_visibility_and_specialization_do_not_partially_save(self):
        endpoint = f'/api/dashboard/class-guides/{self.guide.pk}/'
        original = self.client.get(endpoint).json()
        for extra in ({'is_visible': 'false'}, {'is_visible': 0}, {'spec_id': ''}, {'spec_id': True},
                      {'spec_id': 999}, {'spec_id': 65, 'class_name': 'Priest', 'spec_name': 'Holy'}):
            body = {'expected_updated_at': original['updated_at'], 'title': '不应写入',
                'content_markdown': '不应写入', **extra}
            self.assertEqual(self.client.patch(endpoint, json.dumps(body), content_type='application/json').status_code, 400)
        self.assertEqual(self.client.get(endpoint).json()['updated_at'], original['updated_at'])
        self.assertEqual(ClassGuide.objects.get(pk=self.guide.pk).spec_id, 62)

    def test_hidden_article_still_syncs_without_becoming_visible(self):
        ClassGuide.objects.filter(pk=self.guide.pk).update(archived=True)
        post = source_post(); post['gutenbergBlock'][1]['innerHTML'] += '<p>新来源正文。</p>'
        guide, status = import_post(post, self.url)
        self.assertEqual(status, 'imported')
        self.assertIn('新来源正文', guide.content_markdown)
        self.assertTrue(guide.archived)

    def test_new_article_can_start_hidden(self):
        response = self.client.post('/api/dashboard/class-guides/', json.dumps({'title': '隐藏新文章',
            'slug': 'hidden-new', 'spec_id': 65, 'is_visible': False}), content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(ClassGuide.objects.get(pk=response.json()['id']).archived)

    def test_preview_uses_selected_specialization_without_saving(self):
        endpoint = f'/api/dashboard/class-guides/{self.guide.pk}/'
        with patch('botend.dashboard.class_guides.render_blocks', return_value=[]) as render:
            response = self.client.post(endpoint, json.dumps({'content_markdown': '预览内容', 'spec_id': 257}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.args[2].spec_id, 257)
        self.assertEqual(ClassGuide.objects.get(pk=self.guide.pk).spec_id, 62)

    def test_bundle_preserves_hidden_articles_and_manual_specialization(self):
        folder = Path('tmp/django-tests/guide-visibility-' + uuid.uuid4().hex)
        ClassGuide.objects.filter(pk=self.guide.pk).update(archived=True)
        call_command('audit_class_guides', output_dir=str(folder), verbosity=0)
        ClassGuide.objects.filter(pk=self.guide.pk).update(spec_id=257, class_name='Priest', spec_name='Holy')
        call_command('import_class_guide_drafts', input_dir=str(folder), verbosity=0)
        current = ClassGuide.objects.get(pk=self.guide.pk)
        self.assertEqual(current.spec_id, 257)
        self.assertTrue(current.archived)
        slug = current.slug
        current.slug = 'original-' + slug; current.save(update_fields=['slug'])
        call_command('import_class_guide_drafts', input_dir=str(folder), verbosity=0)
        imported = ClassGuide.objects.get(slug=slug)
        self.assertEqual(imported.spec_id, 62)
        self.assertTrue(imported.archived)

    def test_draft_import_validates_specialization_against_manifest(self):
        folder = Path('tmp/class_guides/tests') / str(uuid.uuid4())
        call_command('audit_class_guides', output_dir=str(folder), verbosity=0)
        path = folder / (self.guide.slug + '-12.1.json')
        draft = json.loads(path.read_text(encoding='utf-8'))
        draft.update(spec_id=257, class_name='Priest', spec_name='Holy')
        path.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesMessage(CommandError, '专精编号与清单不一致'):
            call_command('import_class_guide_drafts', input_dir=str(folder), dry_run=True, verbosity=0)

    def test_tag_edit_conflict_and_source_update_preserve_editor_labels(self):
        base=ClassGuide.objects.get(pk=self.guide.pk)
        data={'expected_updated_at':base.updated_at.isoformat(),'title':base.title,
            'content_markdown':'## 中文攻略\n\n测试正文。','tags':['自选标签']}
        url=f'/api/dashboard/class-guides/{self.guide.id}/'
        self.assertEqual(self.client.patch(url,data=json.dumps(data),content_type='application/json').status_code,200)
        data['tags']=['不应写入']
        self.assertEqual(self.client.patch(url,data=json.dumps(data),content_type='application/json').status_code,409)
        post=source_post();post['gutenbergBlock'][1]['innerHTML']='<p>Updated source.</p>'
        import_post(post,self.url)
        self.assertEqual(list(self.guide.tags.values_list('name',flat=True)), ['自选标签'])

    def test_editing_is_embedded_in_dashboard(self):
        target = f'/dashboard/?section=class-guides&guide={self.guide.id}'
        response = self.client.get(f'/dashboard/class-guides/{self.guide.id}/')
        self.assertRedirects(response, target, fetch_redirect_response=False)
        page = self.client.get(target)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, 'dashboard/index.html')
        self.assertTemplateUsed(page, 'dashboard/class_guides.html')
        self.assertContains(page, 'id="sidebar"')
        self.assertContains(page, 'id="article-markdown"')
        self.assertContains(page, f'data-guide-id="{self.guide.id}"')
        self.assertNotContains(page, '进入攻略工作室')
        self.assertNotContains(page, '<header class="studio-header">')
        self.assertEqual(page.context['dashboard_default_section'], 'class-guides')
        self.assertEqual(page['Cache-Control'], 'private, no-store')
        self.assertEqual(page['X-Robots-Tag'], 'noindex, nofollow')

    def test_portal_guides_require_editor_permission_and_hide_archived(self):
        catalog = '/portal/class-guides/'
        article = f'{catalog}{self.guide.id}/'
        for url in (catalog, article):
            response = Client().get(url)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response['Cache-Control'], 'private, no-store')
        page = self.client.get(catalog)
        self.assertContains(page, article)
        self.assertTemplateUsed(page, 'portal/class_guides.html')
        self.assertContains(page, 'data-spec="62"')
        self.assertContains(page, 'data-cg-tag="团本"')
        self.guide.archived = True
        self.guide.save(update_fields=['archived'])
        self.assertNotContains(self.client.get(catalog), article)
        self.assertEqual(self.client.get(article).status_code, 404)

    def test_portal_article_uses_whole_markdown_and_shared_renderer(self):
        revision = save_article(self.guide.id, '中文整篇攻略', content_markdown='## 中文章节\n\n中文正文。\n\n:::details 补充说明\n隐藏说明\n:::\n', expected_updated_at=ClassGuide.objects.get(pk=self.guide.pk).updated_at)
        endpoint = f'/portal/class-guides/{self.guide.id}/'
        page = self.client.get(endpoint)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, 'shared/class_guide_blocks.html')
        self.assertContains(page, '中文章节')
        self.assertContains(page, '本篇目录')
        self.assertContains(page, '法师 · 奥术')
        self.assertContains(page, f'/dashboard/?section=class-guides&amp;guide={self.guide.id}')
        self.assertContains(page, 'class=Mage&amp;spec=Arcane')
        self.assertEqual(page.context['guide'].id, revision.id)
        self.assertEqual(page.context['toc'][0]['title'], '中文章节')
        self.assertContains(self.client.get(endpoint, {'revision':'bad'}), '中文正文')
        other = ClassGuide.objects.create(title='其他专精', slug='other', spec_id=257, game_version='current')
        other_revision = save_article(other.id, other.title, content_markdown='其他正文', expected_updated_at=other.updated_at)
        self.assertContains(self.client.get(endpoint, {'revision':other_revision.id}), '中文正文')

    def test_portal_catalog_and_reader_preserve_manual_content(self):
        manual = save_article(self.guide.id, '人工保留标题', content_markdown='## 人工章节\n中文内容', expected_updated_at=ClassGuide.objects.get(pk=self.guide.pk).updated_at)
        post = source_post();post['gutenbergBlock'][1]['innerHTML'] = '<p>Changed source.</p>'
        import_post(post, self.url)
        page = self.client.get('/portal/class-guides/')
        self.assertEqual(page.context['rows'][0]['title'], '人工保留标题')
        page = self.client.get(f'/portal/class-guides/{self.guide.id}/')
        self.assertEqual(page.context['guide'].id, manual.id)

    def test_author_card_is_extracted_outside_article_body(self):
        soup = BeautifulSoup('<div class="_Widget_x _Author_abc"><div class="_Author__image_xyz" style="background-image:url(https://example.com/avatar.svg)"></div><span class="_Author__nickname_xyz">作者名</span><span class="_Author__title_xyz">团队成员</span><a href="https://example.com/channel" title="频道"></a></div>', 'html.parser')
        profile = extract_author_profile(soup, {'name':'原始名'})
        self.assertEqual(profile['name'], '作者名')
        self.assertEqual(profile['avatar'], 'https://example.com/avatar.svg')
        self.assertEqual(profile['title'], '团队成员')
        self.assertEqual(profile['links'], [{'label':'频道','url':'https://example.com/channel'}])

    def test_disclaimer_is_shared_by_tag_without_changing_articles(self):
        endpoint = '/api/dashboard/class-guides/disclaimer/'
        original = ClassGuide.objects.get(pk=self.guide.pk).content_markdown
        text = '中文免责声明\n<script>示例仅作为文本</script>'
        response = self.client.patch(endpoint, data=json.dumps({'text': text}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(endpoint).json()['text'], text)
        url = f'/portal/class-guides/{self.guide.id}/'
        page = self.client.get(url)
        self.assertContains(page, '中文免责声明')
        self.assertContains(page, '&lt;script&gt;')
        self.assertNotContains(page, '<script>示例')
        tag = self.guide.tags.get(name='maxroll')
        self.guide.tags.remove(tag)
        self.assertNotContains(self.client.get(url), 'cg-disclaimer')
        self.guide.source_url = ''
        self.guide.save(update_fields=['source_url'])
        self.guide.tags.add(tag)
        self.assertContains(self.client.get(url), '中文免责声明')
        self.client.patch(endpoint, data=json.dumps({'text': ''}), content_type='application/json')
        self.assertNotContains(self.client.get(url), 'cg-disclaimer')
        self.assertEqual(ClassGuide.objects.get(pk=self.guide.pk).content_markdown, original)

    def test_disclaimer_requires_permission_and_valid_text(self):
        endpoint = '/api/dashboard/class-guides/disclaimer/'
        self.assertEqual(Client().get(endpoint).status_code, 403)
        self.assertEqual(Client().patch(endpoint, data=json.dumps({'text': '修改'}), content_type='application/json').status_code, 403)
        for text in (None, [], '字' * 3001):
            self.assertEqual(self.client.patch(endpoint, data=json.dumps({'text': text}), content_type='application/json').status_code, 400)

    def test_author_customization_survives_sync_and_can_restore_source(self):
        source = source_post();source['author_profile'] = {'name':'来源作者','avatar':'https://example.com/source.svg'}
        import_post(source, self.url)
        base = ClassGuide.objects.get(pk=self.guide.pk)
        profile = {'name':'中文编辑','title':'攻略维护者','bio':'第一段简介\n第二段简介','avatar':'https://example.com/custom.png',
                   'links':[{'label':'个人主页','url':'https://example.com/me'}]}
        endpoint = f'/api/dashboard/class-guides/{self.guide.id}/'
        body = {'expected_updated_at':base.updated_at.isoformat(), 'title':base.title,
                'content_markdown':base.content_markdown, 'author_profile':profile}
        response = self.client.patch(endpoint, data=json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        source['author_profile']['name'] = '来源更新'
        import_post(source, self.url)
        page = self.client.get(f'/portal/class-guides/{self.guide.id}/')
        self.assertContains(page, '中文编辑')
        self.assertContains(page, '第一段简介')
        self.assertContains(page, 'https://example.com/me')
        self.assertEqual(page.context['author_profile']['name'], '中文编辑')
        body['author_profile'] = {'name':'过期修改'}
        self.assertEqual(self.client.patch(endpoint, data=json.dumps(body), content_type='application/json').status_code,409)
        self.guide.refresh_from_db()
        self.assertEqual(self.guide.author_profile['name'], '中文编辑')
        body.update(expected_updated_at=self.guide.updated_at.isoformat(), author_profile=None)
        self.assertEqual(self.client.patch(endpoint, data=json.dumps(body), content_type='application/json').status_code,200)
        self.assertEqual(self.client.get(endpoint).json()['display_author_profile']['name'], '来源更新')

    def test_author_customization_rejects_unsafe_links(self):
        base = ClassGuide.objects.get(pk=self.guide.pk)
        body = {'expected_updated_at':base.updated_at.isoformat(), 'title':base.title,
                'content_markdown':base.content_markdown, 'author_profile':{'name':'作者','avatar':'javascript:alert(1)'}}
        self.assertEqual(self.client.patch(f'/api/dashboard/class-guides/{self.guide.id}/', data=json.dumps(body), content_type='application/json').status_code,400)

    def test_save_displays_immediately_without_review_or_history(self):
        endpoint = f'/api/dashboard/class-guides/{self.guide.id}/'
        before = self.client.get(endpoint).json()
        body = {'expected_updated_at': before['updated_at'], 'title': '直接保存的攻略',
                'content_markdown': '## 当前正文\n\n保存后即可阅读 [[spell:30451]]。'}
        create_name(game_version='12.1', kind='spell', object_id=30451,
            name_en='Arcane Blast', name_zh='奥术冲击', evidence='测试元数据')
        response = self.client.patch(endpoint, json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(self.client.get(f'/portal/class-guides/{self.guide.id}/'), '保存后即可阅读')
        self.assertContains(self.client.get(f'/dashboard/class-guides/{self.guide.id}/preview/'), '奥术冲击')
        after = self.client.get(endpoint).json()
        self.assertFalse({'revision', 'revisions', 'revision_number', 'published_revision_id'} & after.keys())
        self.assertEqual(self.client.patch(endpoint, json.dumps(body), content_type='application/json').status_code, 409)
        for action in ['approve', 'restore']:
            self.assertEqual(self.client.patch(endpoint, json.dumps({'action': action,
                'expected_updated_at': after['updated_at']}), content_type='application/json').status_code, 400)

    def test_unsaved_markdown_preview_is_readonly_and_directory_is_derived(self):
        endpoint = f'/api/dashboard/class-guides/{self.guide.id}/'
        response = self.client.post(endpoint, data=json.dumps({'content_markdown': '## 新章节\n\n:::details 说明\n独立正文\n:::'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['toc'][0]['title'], '新章节')
        self.assertIn('独立正文', response.json()['html'])
        base = ClassGuide.objects.get(pk=self.guide.pk)
        response = self.client.patch(endpoint, data=json.dumps({'expected_updated_at':ClassGuide.objects.get(pk=self.guide.pk).updated_at.isoformat(),
            'title': base.title, 'content_markdown': '## 新增中文标题\n\n' + base.content_markdown}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        data = self.client.get(endpoint).json()
        self.assertTrue(data['checks']['untranslated'])
        self.assertNotIn('blocks', data)
        self.assertTrue(data['content_markdown'].startswith('## 新增中文标题'))

    def test_content_checks_warn_without_blocking_save(self):
        current = ClassGuide.objects.get(pk=self.guide.pk)
        self.assertFalse(check_article(current)['complete'])
        saved = save_article(current.pk, '仍可保存', content_markdown='中文正文 [[spell:9999999999]]', expected_updated_at=current.updated_at)
        self.assertTrue(check_article(saved)['unresolved_references'])
        self.assertContains(self.client.get(f'/portal/class-guides/{current.pk}/'), '中文正文')

    def test_translation_is_cached_and_preserves_reference(self):
        blocks = [{'id':'x','type':'html','html':'<p>Cast [[spell:30451]].</p>'}]
        service = Mock(); service.available.return_value = True
        service.engine.send_message.return_value = json.dumps(['⟪HTML00000⟫施放 ⟪REF00001⟫。⟪HTML00002⟫'])
        translated, failed = translate_blocks(blocks, '12.1', service=service)
        self.assertFalse(failed)
        self.assertIn('施放 [[spell:30451]]', translated[0]['html'])
        self.assertEqual(ClassGuideTranslation.objects.count(), 1)
        translate_blocks(blocks, '12.1', service=service)
        self.assertEqual(service.engine.send_message.call_count, 1)

    def test_translation_allows_chinese_reference_order_without_changing_html(self):
        service = Mock(); service.available.return_value = True
        blocks = [{'id':'order','type':'html','html':'<p>Build <strong>three stacks</strong> using [[spell:30451]].</p>'}]
        service.engine.send_message.return_value = json.dumps(['⟪HTML00000⟫施放 ⟪REF00003⟫，积累⟪HTML00001⟫三层⟪HTML00002⟫。⟪HTML00004⟫'])
        result, failed = translate_blocks(blocks, '12.1', service=service)
        self.assertFalse(failed)
        self.assertEqual(result[0]['html'], '<p>施放 [[spell:30451]]，积累<strong>三层</strong>。</p>')

    def test_failed_markup_translation_falls_back_to_visible_nodes(self):
        service = Mock(); service.available.return_value = True
        service.engine.send_message.side_effect = [json.dumps(['破坏了引用的整块译文']), json.dumps(['施放'])]
        blocks = [{'id':'fallback','type':'html','html':'<p>Cast <strong>[[spell:30451]]</strong>.</p>'}]
        result, failed = translate_blocks(blocks, '12.1', service=service)
        self.assertFalse(failed)
        self.assertEqual(result[0]['html'], '<p>施放 <strong>[[spell:30451]]</strong>.</p>')
        self.assertEqual(service.engine.send_message.call_count, 2)
        result, failed = translate_blocks([{'id':'reformatted','type':'html','html':'<div>Cast <em>[[spell:30451]]</em>.</div>'}], '12.1', service=service)
        self.assertFalse(failed)
        self.assertEqual(result[0]['html'], '<div>施放 <em>[[spell:30451]]</em>.</div>')
        self.assertEqual(service.engine.send_message.call_count, 2)

    def test_translation_corruption_is_not_cached(self):
        service = Mock(); service.available.return_value = True
        service.engine.send_message.return_value = json.dumps(['删除引用的错误译文'])
        blocks = [{'id':'x','type':'html','html':'<p>Cast [[spell:30451]].</p>'}]
        result, failed = translate_blocks(blocks, '12.1', service=service)
        self.assertTrue(failed)
        self.assertEqual(ClassGuideTranslation.objects.count(), 0)
        self.assertIn('[[spell:30451]]', result[0]['html'])


class GuideContentTests(SimpleTestCase):
    def test_inline_styles_cannot_split_a_word_for_translation(self):
        value = '<p>your <strong><mark>Vengea</mark><mark>nce Demon Hunter</mark></strong> and you<mark>r </mark>role.</p>'
        self.assertEqual(clean_html(value), '<p>your <strong>Vengeance Demon Hunter</strong> and your role.</p>')

    def test_unsafe_markup_cannot_execute(self):
        result = clean_html('<script>alert(1)</script><img src="javascript:alert(1)" onerror="alert(2)"><a href="javascript:alert(3)">链接</a>')
        for forbidden in ('<script', 'onerror', 'javascript:'):
            self.assertNotIn(forbidden, result)
        rendered = render_references('<p>[[spell:1]]</p>', {'[[spell:1]]': {'name':'<img onerror=alert(1)>','resolved':True}})
        self.assertNotIn('<img onerror', rendered)

    def test_duplicate_blocks_and_invalid_children_rejected(self):
        with self.assertRaises(ValueError):
            validate_blocks([{'id':'x','type':'html'}, {'id':'x','type':'html'}])
        with self.assertRaises(ValueError):
            validate_blocks([{'id':'x','type':'html','children':{}}])

    def test_catalog_uses_observed_links_and_alias(self):
        rows = discover('<a href="/wow/class-guides/devourer-demon-hunter-mythic-guide">攻略</a><a href="/wow/class-guides/fire-mage-leveling-guide">练级</a>')
        self.assertEqual(len(rows), 1)
        self.assertIn('mythic-guide', rows[0])

    def test_unknown_blocks_are_retained_and_flagged(self):
        post = source_post(); post['gutenbergBlock'].append({'blockName':'new/custom','innerHTML':'<p>不能静默丢弃</p>'})
        blocks, audit = convert(post)
        self.assertEqual(len(audit['unsupported']), 1)
        self.assertIn('不能静默丢弃', blocks[-1]['html'])

    def test_snappy_invalid_backreference_and_size_rejected(self):
        for value in (bytes([5, 1, 0]), bytes([255, 255, 255, 127])):
            with self.assertRaises(ValueError):
                snappy(value)

    def test_ads_are_excluded_from_editorial_content(self):
        post = source_post()
        for text in ['[inplace_ad 1]', '[inplace_ad 1]-', '[inplace_ad 2] —']:
            post['gutenbergBlock'].append({'blockName':'core/paragraph','innerHTML':'<p>' + text + '</p>'})
        post['gutenbergBlock'].append({'blockName':'core/paragraph','innerHTML':'<p>有效攻略说明</p>'})
        blocks, _ = convert(post)
        self.assertNotIn('inplace_ad', str(blocks))
        self.assertIn('有效攻略说明', str(blocks))


class GuideMarkdownTests(SimpleTestCase):
    def test_single_document_headings_and_nested_components(self):
        source = "## 概览\n\n说明 **重点** [[spell:30451]]。\n\n:::tabs\n:::tab 团本\n### 单体\n正文\n:::\n:::tab 大秘境\n:::columns\n:::column\n左栏\n:::\n:::column\n右栏\n:::\n:::\n:::\n:::\n"
        blocks = compile_markdown(source)
        self.assertEqual(blocks[0]['data']['source_line'], 0)
        self.assertIn('<strong>重点</strong>', blocks[1]['html'])
        self.assertEqual(blocks[2]['title'], '团本')
        self.assertEqual(blocks[3]['title'], '单体')
        self.assertEqual(blocks[3]['data']['source_line'], 6)
        self.assertEqual(blocks[3]['data']['level'], 4)
        result = compile_markdown(blocks_to_markdown(blocks))
        self.assertIn('右栏', str(result))
        self.assertEqual(result[2]['type'], 'heading')
        self.assertNotIn(':::tab', blocks_to_markdown(blocks))

    def test_vertical_conversion_preserves_nested_components_and_code(self):
        source = ('## 配装\n\n:::tabs\n:::tab 单体\n### 优先级\n'
                  '[[spell:30451]]\n:::tabs 备选\n:::tab 爆发\n'
                  ':::talents\n```json\n{"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"}\n```\n:::\n:::\n:::\n:::\n'
                  ':::tab 多目标\n| 技能 |\n| --- |\n| [[item:123]] |\n:::\n:::\n'
                  '## 宏\n```wow-macro\n:::tab 这是宏内文本\n/cast 奥术冲击\n```\n')
        converted = normalize_tab_markdown(source)
        self.assertIn('### 单体', converted)
        self.assertIn('#### 优先级', converted)
        self.assertIn('### 多目标', converted)
        self.assertIn('## 宏\n```wow-macro\n:::tab 这是宏内文本\n/cast 奥术冲击\n```', converted)
        self.assertIn('"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"', converted)
        self.assertIn('| [[item:123]] |', converted)
        self.assertEqual(normalize_tab_markdown(converted), converted)
        self.assertNotIn("'type': 'tab'", str(compile_markdown(converted)))

    def test_source_converter_expands_all_tabs_in_order(self):
        post = source_post()
        post['gutenbergBlock'] = [
            {'blockName': 'core/heading', 'attributes': {'title': '配置', 'level': 2}},
            {'blockName': 'advgb/adv-tabs', 'innerBlocks': [
                {'blockName': 'advgb/tab', 'attributes': {'title': title}, 'innerBlocks': [
                    {'blockName': 'core/paragraph', 'innerHTML': '<p>' + content + '</p>'}
                ]} for title, content in [('单体', '[[spell:30451]]'), ('多目标', '[[item:123]]')]]
            }]
        blocks, audit = convert(post)
        markdown = blocks_to_markdown(blocks)
        self.assertNotIn(':::tab', markdown)
        self.assertLess(markdown.index('### 单体'), markdown.index('### 多目标'))
        self.assertIn('[[spell:30451]]', markdown)
        self.assertIn('[[item:123]]', markdown)
        self.assertEqual(audit['source_block_counts']['advgb/tab'], 2)

    def test_documents_without_tabs_are_not_reformatted(self):
        source = '## 标题\r\n\r\n正文  \r\n| 表格 |\r\n| --- |\r\n'
        self.assertEqual(normalize_tab_markdown(source), source)

    def test_fences_preserve_literal_directives_and_reference_examples(self):
        blocks = compile_markdown('```text\n:::tabs\n[[spell:30451]]\n```\n\n```wow-macro\n/cast 奥术冲击\n```')
        self.assertIn(':::tabs', blocks[0]['html'])
        self.assertIn('[[spell:30451]]', render_references(blocks[0]['html'], {}))
        self.assertEqual(blocks[1]['data']['code'], '/cast 奥术冲击')

    def test_invalid_extensions_and_html_are_handled(self):
        for source in [':::tabs\n缺少闭合', ':::', ':::invalid\n:::']:
            with self.assertRaises(ValueError):
                compile_markdown(source)
        rendered = str(compile_markdown('## 标题\n\n<script>alert(1)</script>\n\n![图片](javascript:alert(1))'))
        self.assertNotIn('<script', rendered)
        self.assertNotIn('src="javascript:', rendered)

    def test_table_and_talent_source_roundtrip(self):
        source = '| 技能 | 用途 |\n| --- | --- |\n| [[spell:30451]] | 单体 |\n\n:::talents 团本\n```json\n{"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"}\n```\n配合爆发使用。\n:::'
        blocks = compile_markdown(source)
        self.assertIn('<table>', blocks[0]['html'])
        self.assertTrue(blocks[1]['data']['converted'])
        self.assertIn('配合爆发使用', str(compile_markdown(blocks_to_markdown(blocks))))


class GuideTranslationProtectionTests(SimpleTestCase):
    def test_ordinary_lowercase_words_are_not_forced_to_skill_names(self):
        from botend.services.class_guide_glossary import GuideGlossary
        glossary = GuideGlossary.from_trusted_pairs([('Heal','治疗术'),('Cleaner','清洁工'),('Devour Magic','吞噬魔法')])
        value = 'You can heal with Heal and use Devour Magic for a cleaner display.'
        protected = glossary.protect(value)
        restored = glossary.restore(protected.text, protected.replacements)
        self.assertEqual(restored, 'You can heal with 治疗术 and use 吞噬魔法 for a cleaner display.')
        self.assertTrue(glossary.needs_new_translation(value))

    def test_macro_translation_keeps_full_names_conditions_and_lua(self):
        from botend.services.class_guide_macros import localize_macro, macro_name_map
        names = macro_name_map([('spell','Corruption','腐化'), ('spell','Corruption','腐蚀术')])
        self.assertNotIn('corruption', names)
        names = macro_name_map([('spell','Corruption','腐化'), ('spell','Corruption','腐蚀术'), ('macro','Corruption','腐蚀术')])
        self.assertEqual(names['corruption'], '腐蚀术')
        code = '#showtooltip Devour Magic\n/cast [@focus] Devour Magic;Spell Lock\n/cast [known:Shadowfury] Shadowfury\n/use 13\n/run if not IsSpellKnown(108503) then PetDismiss(); end'
        result, missing = localize_macro(code, {'devour magic':'吞噬魔法','spell lock':'法术封锁','shadowfury':'暗影之怒'})
        self.assertFalse(missing)
        self.assertIn('/cast [@focus] 吞噬魔法;法术封锁', result)
        self.assertIn('/cast [known:暗影之怒] 暗影之怒', result)
        self.assertIn('/use 13', result)
        self.assertIn('/run if not IsSpellKnown(108503) then PetDismiss(); end', result)
        unchanged, missing = localize_macro('/cast Unknown Spell', {})
        self.assertEqual(unchanged, '/cast Unknown Spell')
        self.assertEqual(missing, ['Unknown Spell'])
        result, missing = localize_macro('/use item:212263\n/cast !Spinning Crane Kick\n/cancelaura Levitate\n/cast [stance:2] Defensive Stance; stance[:3] Battle Stance', {'spinning crane kick':'神鹤引项踢','levitate':'漂浮术','defensive stance':'防御姿态','battle stance':'战斗姿态'})
        self.assertFalse(missing)
        self.assertIn('/use item:212263', result)
        self.assertIn('/cast !神鹤引项踢', result)
        self.assertIn('/cancelaura 漂浮术', result)
        self.assertIn('; [stance:3] 战斗姿态', result)

    def test_nested_emphasis_does_not_generate_repeated_markers(self):
        result = html_to_markdown('<p><strong><strong>痛苦</strong> 术士 </strong>正文</p>')
        self.assertEqual(result, '**痛苦 术士** 正文')

    def test_terms_do_not_modify_links_images_or_reference_parameters(self):
        glossary = WowNewsGlossary.from_trusted_pairs([('Affliction','痛苦'),('Raid','团队副本')])
        source = '<p>Affliction <a href="https://example.com/raid">Raid</a><img src="https://example.com/affliction.webp"> [[spell:123@Raid]] Affliction</p>'
        protected = protect_guide_text(source, glossary)
        restored = glossary.restore(protected.text, protected.replacements)
        self.assertIn('https://example.com/raid', restored)
        self.assertIn('https://example.com/affliction.webp', restored)
        self.assertIn('[[spell:123@Raid]]', restored)
        self.assertEqual(restored.count('痛苦'), 2)
        self.assertIn('>团队副本</a>', restored)
        self.assertTrue(protected.is_intact(protected.text))


class GuideSourceRepairTests(SimpleTestCase):
    def test_malformed_source_reference_keeps_both_skills_and_connector(self):
        references = {}
        source = '<span class="wow-trait" data-wow-id="60103">Lava Lash[/wow-spell] during [wow-trait id=101809 level=2]Hot Hand</span>'
        result = clean_html(source, references)
        self.assertEqual(result, '[[spell:60103]] during [[talent:101809]]')
        self.assertEqual(references['[[spell:60103]]']['source_name'], 'Lava Lash')
        self.assertEqual(references['[[talent:101809]]']['source_name'], 'Hot Hand')

    def test_empty_rotation_reference_does_not_create_spell_zero(self):
        from botend.services.class_guide_codec import component_html
        rendered = component_html('rotation', {'entries': [{'type':'spell','id':0,'label':'等待能量恢复'}]})
        self.assertNotIn('[[spell:0]]', rendered)
        self.assertIn('等待能量恢复', rendered)
