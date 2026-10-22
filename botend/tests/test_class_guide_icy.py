"""用原创页面样例验证 Icy 正文结构适配，不保存第三方攻略正文。"""

import json
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.test import SimpleTestCase

from botend.services.class_guide_codec import Reader
from botend.services.class_guide_content import REF_RE, walk_blocks, resolve_references
from botend.services.class_guide_icy import convert_page, merge_chapters, localize_macro_blocks


BASE = 'https://www.icy-veins.com/wow/blood-death-knight-pve-tank-'
CODE = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'


def page(body):
    return '<main><div class="guide-page-content">' + body + '</div></main>'


class IcyContentConversionTests(SimpleTestCase):
    def test_editor_html_comments_are_not_published_as_article_text(self):
        source = page('<!-- 作者编辑备注 --><table><!-- 内部列宽提示 -->'
                      '<tr><td>完整装备说明。</td></tr></table>')
        result = convert_page(source, BASE + 'gear-best-in-slot')
        self.assertNotIn('作者编辑备注', result['content_markdown'])
        self.assertNotIn('内部列宽提示', result['content_markdown'])
        self.assertIn('完整装备说明。', result['content_markdown'])

    def test_plain_paragraph_game_export_is_talent_component(self):
        code = 'C4DA' + 'A' * 50
        result = convert_page(page('<p>' + code + '</p>'), BASE + 'spec-builds-talents')
        self.assertEqual([b['data']['code'] for b in walk_blocks(result['blocks'])
                          if b['type'] == 'talents'], [code])

    def test_source_calculator_hash_uses_verified_native_export_and_keeps_original(self):
        source_hash = '#mB-ABCDE-FGHIJ-AAAA-KLMNO-'
        source = page('<div class="midnight-skill-builder-embed" id="midnight-skill-builder-1"></div>'
                      f'<script>new MidnightTalentCalculator("midnight-skill-builder-1","{source_hash}",{{}});</script>')
        result = convert_page(source, BASE + 'spec-builds-talents', {source_hash: {'code': CODE}})
        codes = [b['data']['code'] for b in walk_blocks(result['blocks']) if b['type'] == 'talents']
        self.assertEqual(codes, [CODE])
        self.assertIn(source_hash, result['content_markdown'])
        self.assertIn('来源计算器编码', result['content_markdown'])

    def test_plain_macro_lists_and_focus_paragraph_are_protected_as_code(self):
        source = page('<p>原创宏说明。</p><ul><li>/stopcasting</li>'
                      '<li>/cast [@focus] Counterspell</li></ul><p>/focus [@mouseover]</p>')
        result = convert_page(source, BASE + 'mythic-plus-tips')
        codes = [b['data']['code'] for b in walk_blocks(result['blocks']) if b['type'] == 'code']
        self.assertEqual(codes, ['/stopcasting\n/cast [@focus] Counterspell', '/focus [@mouseover]'])
        self.assertIn('原创宏说明。', result['content_markdown'])

    def test_hidden_talent_choices_export_strings_and_calculator(self):
        source = page(f'''
          <div class="table-of-contents">目录导航应排除</div>
          <h2>完整天赋说明</h2><p>原创示例的完整正文。</p>
          <div class="export-string-wrapper"><details>
            <summary><span class="export-string__title">单体天赋</span></summary>
            <div><span class="export-string__code">{CODE}</span><p>额外解释必须保留。</p></div>
          </details><button>复制</button></div>
          <div class="filter-content-wrapper selected" data-filter-index="0-0"><p>默认方案解释。</p></div>
          <div class="filter-content-wrapper" data-filter-index="0-1"><p>未选中方案也有完整解释。</p></div>
          <div class="talent-calculator-wrapper" data-filter-index="0-1">
            <div class="midnight-skill-builder-embed" id="midnight-skill-builder-1"></div>
          </div>
          <script>new MidnightTalentCalculator('midnight-skill-builder-1','{CODE}',{{}});</script>
          <div class="inline-author-block">作者导航应排除</div>''')
        result = convert_page(source, BASE + 'spec-builds-talents')
        text = result['content_markdown']
        self.assertNotIn('目录导航应排除', text)
        self.assertNotIn('作者导航应排除', text)
        self.assertNotIn('<script', text)
        for value in ('完整天赋说明', '额外解释必须保留', '默认方案解释', '未选中方案也有完整解释'):
            self.assertIn(value, text)
        talents = [b for b in walk_blocks(result['blocks']) if b['type'] == 'talents']
        self.assertEqual([b['data']['code'] for b in talents], [CODE, CODE])
        self.assertFalse(result['issues'])

    def test_direct_export_details_macro_is_code_not_translatable_prose(self):
        macro = '#showtooltip Arcane Surge\n/cast [@focus,harm] Arcane Surge\n/run print("keep Lua")'
        result = convert_page(page(f'''<details class="export-string"><summary>
          <span class="export-string__title">爆发宏</span></summary><div class="export-string__body">
          <div class="export-string__code_large_macro">{macro}</div></div></details>'''), BASE + 'macros-addons')
        blocks = list(walk_blocks(result['blocks']))
        self.assertEqual(next(b for b in blocks if b['type'] == 'code')['data']['code'], macro)
        localized, notes = localize_macro_blocks(result['blocks'], [
            {'kind': 'spell', 'name_en': 'Arcane Surge', 'name_zh': '奥术涌动'}])
        code = next(b for b in walk_blocks(localized) if b['type'] == 'code')['data']['code']
        self.assertIn('/cast [@focus,harm] 奥术涌动', code)
        self.assertIn('/run print("keep Lua")', code)
        self.assertFalse(notes)

    def test_grid_gear_keeps_distinct_variants_and_acquisition_notes(self):
        result = convert_page(page('''
          <div class="image_block_header_buttons"><span id="bis_0_0_button">团本装备</span><span id="bis_0_1_button">地下城装备</span></div>
          <div class="image_block_content selected" id="bis_0_0"><div class="bis_items_grid">
            <div class="bis_item"><span data-iv-tooltip="wow/item/123?bonus=100:200&amp;original=456">示例装备</span><span class="bis_item_slot">头部</span><div class="bis_item_footer">测试首领掉落</div></div>
          </div></div>
          <div class="image_block_content" id="bis_0_1"><div class="bis_items_grid">
            <div class="bis_item"><span data-iv-tooltip="wowxptr/item/123?bonus=300&amp;ilvl=650&amp;level=90">示例装备</span><span class="bis_item_slot">头部</span><div class="bis_item_footer">另一个来源</div></div>
          </div></div>'''), BASE + 'gear-best-in-slot')
        refs = result['source_refs']
        self.assertEqual(len(refs), 2)
        decoded = [Reader(ref['variant']).item(ref['id']) for ref in refs.values()]
        self.assertEqual([value['bonuses'] for value in decoded], [[100, 200], [300]])
        self.assertEqual(decoded[1]['itemLevel'], 650)
        self.assertEqual(decoded[1]['level'], 90)
        self.assertEqual(list(refs.values())[0]['source_parameters']['original'], ['456'])
        self.assertEqual(list(refs.values())[1]['source_branch'], 'wowxptr')
        self.assertIn('测试首领掉落', result['content_markdown'])
        self.assertIn('另一个来源', result['content_markdown'])
        self.assertIn('地下城装备', result['content_markdown'])
        self.assertEqual(len(REF_RE.findall(result['content_markdown'])), 2)

    def test_rotation_keeps_nested_lists_hidden_alternatives_and_conditions(self):
        source = page('''
          <div class="rotation_switch" id="rotation_switch_talent-2"><label>测试天赋<input type="checkbox"></label></div>
          <span id="rotation_build_0_button">单体优先级</span><span id="rotation_build_1_button">范围优先级</span>
          <div id="rotation_build_0" class="rotation_block_content selected"><ol>
            <li class="rotation_line_talent-2_on">第一步<span data-wowhead="spell=12345">测试技能</span><ol><li>嵌套条件动作</li></ol></li>
            <li class="rotation_line_talent-2_off">未选天赋时的第二步</li>
          </ol></div>
          <div id="rotation_build_1" class="rotation_block_content"><p>隐藏的范围循环也必须保留。</p></div>''')
        result = convert_page(source, BASE + 'rotation-cooldowns-abilities')
        text = result['content_markdown']
        for value in ('单体优先级', '范围优先级', '测试天赋（启用）', '测试天赋（未启用）',
                      '嵌套条件动作', '隐藏的范围循环也必须保留', '[[spell:12345]]'):
            self.assertIn(value, text)
        self.assertEqual(len(result['conditions']), 2)
        self.assertFalse(result['issues'])

    def test_level_slider_sections_keep_inclusive_ranges_and_minimum_levels(self):
        result = convert_page(page('''
          <div class="leveling-slider-wrapper"><input type="range" min="1" max="90"></div>
          <ul><li class="arc_level_2_9">低等级动作。</li><li class="arc_level_20">高等级动作。</li></ul>'''),
          'https://www.icy-veins.com/wow/arcane-mage-leveling-guide')
        self.assertIn('等级 2–9：', result['content_markdown'])
        self.assertIn('等级 20 起：', result['content_markdown'])
        self.assertIn('低等级动作', result['content_markdown'])
        self.assertIn('高等级动作', result['content_markdown'])
        self.assertEqual(result['conditions'][0]['level_max'], 9)

    def test_full_changelog_remains_available_in_existing_details_format(self):
        result = convert_page(page('<h2>更新记录</h2><div class="changelog_wrapper"><ul><li>较早更新的完整正文。</li><li>最新更新的完整正文。</li></ul></div>'), BASE + 'guide')
        self.assertIn('较早更新的完整正文', result['content_markdown'])
        self.assertIn('最新更新的完整正文', result['content_markdown'])
        self.assertEqual(next(b for b in result['blocks'] if b['type'] == 'details')['title'], '历史更新记录')

    def test_tables_details_images_macros_and_links_survive(self):
        macro = '#showtooltip\n/cast 测试技能\n/say :::'
        source = page(f'''
          <details><summary>完整解释</summary><p>保留折叠说明。</p></details>
          <fieldset><legend>装备对比</legend><p>保留对比正文。</p></fieldset>
          <table><tr><th colspan="2">合并表头</th></tr><tr><td rowspan="2">第一列</td><td>第二列</td></tr><tr><td>第三行</td></tr></table>
          <pre>{macro}</pre><img src="//example.com/diagram.png" alt="测试示意图">
          <p><a href="./page#detail" onclick="alert(1)">章节链接</a></p>''')
        result = convert_page(source, BASE + 'guide')
        self.assertIn(macro, result['content_markdown'])
        combined = ''.join(b.get('html', '') for b in walk_blocks(result['blocks']))
        soup = BeautifulSoup(combined, 'html.parser')
        self.assertEqual(soup.find('th')['colspan'], '2')
        self.assertEqual(soup.find('td')['rowspan'], '2')
        self.assertIn('https://example.com/diagram.png', result['content_markdown'])
        self.assertIn('https://www.icy-veins.com/wow/page#detail', result['content_markdown'])
        self.assertNotIn('onclick', result['content_markdown'])
        self.assertEqual(len([b for b in walk_blocks(result['blocks']) if b['type'] == 'details']), 2)

    def test_missing_dynamic_content_is_reported(self):
        result = convert_page(page('''<p>说明正文。</p>
          <div class="midnight-skill-builder-embed" id="missing-tree"></div>
          <canvas></canvas>
          <ol><li class="rotation_line_unknown_on">条件动作。</li></ol>'''), BASE + 'guide')
        self.assertEqual({entry['type'] for entry in result['issues']},
                         {'计算器缺少静态构筑代码', '动态内容需要补充', '条件名称缺失'})

    def test_calculator_compact_code_is_not_treated_as_game_import_string(self):
        compact = '250#' + 'A' * 40 + '-1'
        result = convert_page(page(f'''<p>完整天赋说明。</p>
          <div class="midnight-skill-builder-embed" id="midnight-skill-builder-1"></div>
          <script>new MidnightTalentCalculator('midnight-skill-builder-1','{compact}',{{}});</script>'''), BASE + 'spec-builds-talents')
        self.assertIn(compact, result['content_markdown'])
        self.assertFalse(any(b['type'] == 'talents' for b in walk_blocks(result['blocks'])))
        self.assertFalse(result['issues'])

    def test_currency_affixes_and_source_videos_keep_accessible_targets(self):
        result = convert_page(page('''<p><span data-iv-tooltip="wow/currency/123">测试货币</span>
          <span data-iv-tooltip="wow/affix/45">测试词缀</span></p>
          <video><source src="//example.com/demo.webm"></video>
          <iframe src="https://www.youtube.com/embed/example"></iframe>'''), BASE + 'guide')
        for url in ('https://www.wowhead.com/currency=123', 'https://www.wowhead.com/mythic-affix=45',
                    'https://example.com/demo.webm', 'https://www.youtube.com/embed/example'):
            self.assertIn(url, result['content_markdown'])
        self.assertFalse(result['issues'])

    def test_source_translation_is_readable_without_claiming_official_resolution(self):
        blocks = [{'id': 'sample', 'type': 'html', 'html': '<p>[[spell:123]]</p>', 'children': []}]
        with patch('botend.services.class_guide_content.names_for', return_value=[]), \
             patch('botend.services.class_guide_content._spell_snapshot_metadata', return_value={}), \
             patch('botend.services.wow_spell_display.SpellDisplayReader'):
            result = resolve_references(blocks, '12.1.5', source_refs={
                '[[spell:123]]': {'source_name': 'Sample Spell', 'source_name_zh': '示例技能'}})
        self.assertEqual(result['[[spell:123]]']['name'], '示例技能')
        self.assertFalse(result['[[spell:123]]']['resolved'])

    def test_unrepresentable_item_bonus_keeps_source_parameters_and_reports_issue(self):
        result = convert_page(page('<p><span data-iv-tooltip="wow/item/123?bonus=1221413836:10">测试装备</span></p>'), BASE + 'gear-best-in-slot')
        self.assertEqual(result['issues'][0]['type'], '物品变体无法解析')
        self.assertEqual(result['source_refs']['[[item:123]]']['source_parameters']['bonus'], ['1221413836:10'])

    def test_merging_keeps_chapters_stable_ids_and_all_reference_contexts(self):
        result = merge_chapters([
            {'title': '天赋', 'source_url': BASE + 'spec-builds-talents',
             'html': page('<h2>天赋小节</h2><p><span data-iv-tooltip="wow/spell/123">测试技能</span>完整说明。</p>')},
            {'title': '循环', 'source_url': BASE + 'rotation-cooldowns-abilities',
             'html': page('<h2>循环小节</h2><p><span data-iv-tooltip="wowptr/spell/123">测试技能</span>完整步骤。</p>')},
        ])
        self.assertIn('## 天赋', result['content_markdown'])
        self.assertIn('### 循环小节', result['content_markdown'])
        ids = [b['id'] for b in walk_blocks(result['blocks'])]
        self.assertEqual(len(ids), len(set(ids)))
        ref = result['check_data']['source_refs']['[[spell:123]]']
        self.assertEqual(len(ref['source_contexts']), 2)
        self.assertEqual(len(result['chapters']), 2)
        self.assertRaises(ValueError, merge_chapters, [])
        self.assertRaises(ValueError, convert_page, '<html></html>', BASE + 'guide')

    def test_management_command_emits_translation_fields_without_code(self):
        manifest = {'spec_id': 250, 'game_version': '12.1.5',
                    'chapters': [{'title': '天赋', 'source_url': BASE + 'spec-builds-talents', 'file': 'talents.html'}]}
        source = page(f'<p>待翻译的完整正文。</p><div class="export-string-wrapper"><span class="export-string__code">{CODE}</span></div>')
        writes = {}

        def read(path, **kwargs):
            return json.dumps(manifest) if path.name == 'manifest.json' else source

        with patch.object(Path, 'exists', return_value=False), \
             patch.object(Path, 'read_text', read), \
             patch.object(Path, 'stat') as stat, \
             patch.object(Path, 'mkdir'), \
             patch.object(Path, 'write_text', lambda path, value, **kw: writes.update({path.name: value})):
            stat.return_value.st_size = 1000
            call_command('prepare_icy_class_guide', input_manifest='dataset/manifest.json', output_dir='dataset/converted')
        result = json.loads(writes['conversion.json'])
        self.assertEqual(result['spec_id'], 250)
        self.assertEqual(result['tags'], ['icy', '12.1.5'])
        self.assertNotIn(CODE, json.dumps(result['translation_fields']))
        self.assertIn(CODE, writes['source.md'])
        self.assertIn('正文格式已转换', result['status'])


class IcyFullTranslationTests(SimpleTestCase):
    def test_process_backup_uses_requested_flash_model_without_changing_local_config(self):
        from types import SimpleNamespace
        from scripts.build_icy_full_bundle import translation_settings
        original = {'enabled': False, 'api_key': 'test-only', 'model': 'old-model', 'base_url': 'https://example.com'}
        local = SimpleNamespace(AI_PROVIDER_USE_DATABASE=False, ZHIYU_API_CONFIG=original,
                                MIMO_API_CONFIG={'enabled': True, 'priority': 0}, CODEX_API_CONFIG={})
        with patch('scripts.build_icy_full_bundle.log'), patch('scripts.build_icy_full_bundle.importlib.import_module', return_value=local):
            settings = translation_settings(True)
        self.assertEqual(settings['ZHIYU_API_CONFIG']['model'], 'GLM-5.3-Flash')
        self.assertTrue(settings['ZHIYU_API_CONFIG']['enabled'])
        self.assertLess(settings['MIMO_API_CONFIG']['priority'], settings['ZHIYU_API_CONFIG']['priority'])
        self.assertFalse(original['enabled'])
        self.assertEqual(original['model'], 'old-model')

    def test_missing_or_changed_numeric_facts_are_rejected(self):
        translator = self.translator([{'id': 0, 'text': '持续10秒。'}])
        with patch.object(translator, 'save') as save, patch('scripts.build_icy_full_bundle.dump'):
            self.assertEqual(translator.batch(['Lasts 12 seconds.'], attempt=2), {})
        save.assert_not_called()

    def test_literal_links_macro_arguments_and_game_exports_do_not_need_translation(self):
        translator = self.translator([])
        for value in ('https://example.com/addon', '/petdismiss', '@mouseover',
                      '@INSERTPLAYERNAMEHERE', 'C4DA' + 'A' * 50):
            self.assertFalse(translator.should_translate(value), value)

    def translator(self, answer):
        from types import SimpleNamespace
        from scripts.build_icy_full_bundle import FileTranslator
        translator = FileTranslator(Path('unused-translation-cache'), [])
        translator.local.service = SimpleNamespace(
            engine=SimpleNamespace(send_message=lambda *a, **kw: json.dumps(answer, ensure_ascii=False)),
            last_error='')
        return translator

    def test_translation_allows_reference_reordering_but_preserves_every_reference(self):
        source = 'Use [[spell:1]] after [[spell:2]].'
        translator = self.translator([{'id': 0, 'text': '⟪S0000_K00001⟫之后使用⟪S0000_K00000⟫。'}])
        with patch.object(translator, 'save') as save, patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch([source])
        self.assertEqual(result[source], '[[spell:2]]之后使用[[spell:1]]。')
        save.assert_called_once()

    def test_translation_response_ids_can_reorder_without_mixing_segments(self):
        sources = ['Use [[spell:1]].', 'Then [[spell:2]].']
        translator = self.translator([
            {'id': 1, 'text': '然后使用⟪S0001_K00000⟫。'},
            {'id': 0, 'text': '使用⟪S0000_K00000⟫。'}])
        with patch.object(translator, 'save'), patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch(sources)
        self.assertEqual(result[sources[0]], '使用[[spell:1]]。')
        self.assertEqual(result[sources[1]], '然后使用[[spell:2]]。')

    def test_foreign_segment_marker_is_rejected(self):
        sources = ['Use [[spell:1]].', 'Then [[spell:2]].']
        translator = self.translator([
            {'id': 0, 'text': '使用⟪S0001_K00000⟫。'},
            {'id': 1, 'text': '然后使用⟪S0001_K00000⟫。'}])
        with patch.object(translator, 'save'), patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch(sources, attempt=2)
        self.assertNotIn(sources[0], result)
        self.assertIn(sources[1], result)

    def test_unindexed_response_is_rejected(self):
        translator = self.translator(['未编号的中文。'])
        with patch.object(translator, 'save') as save, patch('scripts.build_icy_full_bundle.dump'):
            self.assertEqual(translator.batch(['An independent sentence.'], attempt=2), {})
        save.assert_not_called()

    def test_truncated_response_keeps_only_complete_numbered_items(self):
        translator = self.translator([])
        translator.local.service.engine.send_message = lambda *a, **kw: '[{"id":0,"text":"中文第一项。"},{"id":1,"text":'
        with patch.object(translator, 'save'), patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch(['First.', 'Second.'], attempt=2)
        self.assertEqual(result, {'First.': '中文第一项。'})

    def test_malformed_middle_item_does_not_discard_later_complete_items(self):
        translator = self.translator([])
        translator.local.service.engine.send_message = lambda *a, **kw: '[{"id":0,"text":"第一项。"},{"id":1,"text":坏数据},{"id":"2","text":"第三项。"}]'
        with patch.object(translator, 'save'), patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch(['First.', 'Second.', 'Third.'], attempt=2)
        self.assertEqual(result, {'First.': '第一项。', 'Third.': '第三项。'})

    def test_translation_rejects_reordered_html_tags(self):
        source = '<p>Original explanation.</p>'
        translator = self.translator([{'id': 0, 'text': '⟪S0000_K00001⟫中文说明。⟪S0000_K00000⟫'}])
        with patch.object(translator, 'save') as save, patch('scripts.build_icy_full_bundle.dump'):
            result = translator.batch([source], attempt=2)
        self.assertEqual(result, {})
        save.assert_not_called()

    def test_local_assembly_protects_code_and_markup(self):
        source = '<p>Use [[spell:1]] after [[spell:2]].</p><pre><code>/cast Original</code></pre>'
        translator = self.translator([])
        self.assertEqual(translator.segments(source), ['Use [[spell:1]] after [[spell:2]].'])
        with patch.object(translator, 'cached', return_value='[[spell:2]]之后使用[[spell:1]]。'):
            result = translator.assemble(source)
        self.assertIn('/cast Original', result)
        self.assertIn('<p>[[spell:2]]之后使用[[spell:1]]。</p>', result)
