"""从版本化客户端名称快照和中文 Tooltip 补齐攻略引用，保留核对依据。"""

import csv
import hashlib
import json
import re
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.guide_models import ClassGuide, ClassGuideTerm
from botend.services.class_guide_content import REF_RE, walk_blocks
from botend.constants.hero_talents import HERO_SUBTREE_NAME_ZH
from botend.services.class_guide_macros import localize_macro, macro_name_map


def canonical(value):
    return re.sub(r'\s+', ' ', str(value or '').strip('[] ').replace('’', "'")).casefold()


class Command(BaseCommand):
    help = '补齐当前攻略引用的中文名称与图标；不使用模型生成的名称作为官方校订'

    def add_arguments(self, parser):
        parser.add_argument('--game-version', required=True)
        parser.add_argument('--spell-names-dir', required=True, help='包含同一构建中英文 SpellName CSV 的目录')
        parser.add_argument('--snapshot-build', required=True)
        parser.add_argument('--maxroll-data', help='来源天赋工具的数据快照，用于精确关联天赋条目 ID 和技能 ID')
        parser.add_argument('--talent-names', help='含 class_id、en、zh、spell_id、icon 的中英文天赋对照 JSON；按职业和完整名称消歧')
        parser.add_argument('--historical-snapshot', action='append', default=[], metavar='构建号=目录',
            help='仅为原文中的旧引用核对历史名称，不把历史数据当作当前技能可用性的依据')
        parser.add_argument('--journal-names-dir', help='同构建的 JournalEncounter 与 JournalEncounterCreature 中英文 CSV 目录')
        parser.add_argument('--tooltip-env', type=int, choices=[1, 2], default=1)
        parser.add_argument('--fetch-tooltips', action='store_true')
        parser.add_argument('--workers', type=int, choices=range(1, 9), default=6)
        parser.add_argument('--cache-dir', default='tmp/class_guides/term-toolips')

    def handle(self, *args, **options):
        version, build = options['game_version'], options['snapshot_build']
        if not (build == version or build.startswith(version + '.')):
            raise CommandError('快照构建号必须属于所选游戏版本')
        folder = Path(options['spell_names_dir'])
        tables, digests = {}, {}
        for locale in ['enUS', 'zhCN']:
            path = folder / ('SpellName_' + locale + '.csv')
            digests[locale] = hashlib.sha256(path.read_bytes()).hexdigest()
            with path.open(encoding='utf-8-sig', newline='') as stream:
                tables[locale] = {int(row['ID']): row['Name_lang'] for row in csv.DictReader(stream)}
        names = defaultdict(list)
        spell_icons = {}
        misc_path = folder / 'SpellMisc.csv'
        if misc_path.exists():
            with misc_path.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row.get('DifficultyID') == '0' and int(row.get('SpellIconFileDataID') or 0):
                        spell_icons[int(row['SpellID'])] = int(row['SpellIconFileDataID'])
        for spell_id, english in tables['enUS'].items():
            chinese = tables['zhCN'].get(spell_id, '')
            if re.search(r'[\u3400-\u9fff]', chinese):
                names[canonical(english)].append((spell_id, chinese))
        game_data, entries, hero_entries, hero_icons = {}, {}, {}, {}
        if options['maxroll_data']:
            game_data = json.loads(Path(options['maxroll_data']).read_text(encoding='utf-8'))
            if not str(game_data.get('version', '')).startswith(version + '.'):
                raise CommandError('来源天赋数据不属于该游戏版本')
            for trees in game_data.get('classTrees', {}).values():
                for tree in trees:
                    if not str(tree.get('Version', game_data.get('version', ''))).startswith(version + '.'):
                        continue
                    for node in tree.get('Nodes', {}).values():
                        for entry in node.get('Entries', []):
                            entries[int(entry['ID'])] = entry.get('Definition', {})
                            subtree = tree.get('SubTrees', {}).get(str(entry.get('TraitSubTreeID')), {})
                            if subtree.get('Name_lang'):
                                hero_entries[int(entry['ID'])] = subtree['Name_lang']
                                if subtree.get('UiTextureAtlasElementID'):
                                    hero_icons[int(entry['ID'])] = 'https://assets-ng.maxroll.gg/wow/icons/sprites/{}.webp'.format(int(subtree['UiTextureAtlasElementID']))
        talent_names = json.loads(Path(options['talent_names']).read_text(encoding='utf-8')) if options['talent_names'] else []
        historical = []
        for value in options['historical_snapshot']:
            historical_build, separator, directory = value.partition('=')
            if not separator or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', historical_build):
                raise CommandError('历史快照格式必须为四段构建号=目录')
            directory = Path(directory)
            old = {'build': historical_build, 'tables': {}, 'icons': {}, 'entries': {}, 'digests': {}}
            for locale in ('enUS', 'zhCN'):
                path = directory / ('SpellName_' + locale + '.csv')
                old['digests'][locale] = hashlib.sha256(path.read_bytes()).hexdigest()
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    old['tables'][locale] = {int(row['ID']): row['Name_lang'] for row in csv.DictReader(stream)}
            old_misc = directory / 'SpellMisc_enUS.csv'
            if old_misc.exists():
                with old_misc.open(encoding='utf-8-sig', newline='') as stream:
                    old['icons'] = {int(row['SpellID']): int(row['SpellIconFileDataID']) for row in csv.DictReader(stream)
                        if row.get('DifficultyID') == '0' and int(row.get('SpellIconFileDataID') or 0)}
            entry_path, definition_path = directory / 'TraitNodeEntry_enUS.csv', directory / 'TraitDefinition_enUS.csv'
            if entry_path.exists() and definition_path.exists():
                with definition_path.open(encoding='utf-8-sig', newline='') as stream:
                    definitions = {int(row['ID']): int(row['SpellID']) for row in csv.DictReader(stream)}
                with entry_path.open(encoding='utf-8-sig', newline='') as stream:
                    old['entries'] = {int(row['ID']): definitions.get(int(row['TraitDefinitionID'])) for row in csv.DictReader(stream)}
            historical.append(old)
        class_ids = {'warrior':1, 'paladin':2, 'hunter':3, 'rogue':4, 'priest':5, 'deathknight':6,
            'shaman':7, 'mage':8, 'warlock':9, 'monk':10, 'druid':11, 'demonhunter':12, 'evoker':13}
        refs, source_documents, source_macros = {}, [], []
        for guide in ClassGuide.objects.filter(game_version=version, archived=False):
            revision = guide.revisions.first()
            if not revision:
                continue
            source_documents.append(revision.source_markdown)
            source_refs = revision.audit.get('source_refs', {})
            source_macros.extend(block.get('data', {}).get('code', '') for block in walk_blocks(revision.source_blocks) if block['type'] == 'code')
            for block in walk_blocks(revision.blocks):
                for match in REF_RE.finditer(block.get('html', '') + block.get('title', '')):
                    key = (match[1], int(match[2]))
                    row = refs.setdefault(key, {'kind': key[0], 'id': key[1], 'source_names': set(), 'classes':set()})
                    row['classes'].add(class_ids.get(guide.class_name))
                    english = source_refs.get(match[0], {}).get('source_name')
                    if english:
                        row['source_names'].add(english)
        evidence = '攻略客户端快照 {}；zhCN SHA256 {}；enUS SHA256 {}'.format(build, digests['zhCN'], digests['enUS'])
        created = 0
        if options['journal_names_dir']:
            journal_folder = Path(options['journal_names_dir'])
            source_text = '\n'.join(source_documents)
            for table in ('JournalEncounter', 'JournalEncounterCreature'):
                paths = {locale:journal_folder / (table + '_' + locale + '.csv') for locale in ('enUS', 'zhCN')}
                localized = {}
                for locale, path in paths.items():
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        localized[locale] = {row['ID']:row['Name_lang'] for row in csv.DictReader(stream)}
                for record_id, english_name in localized['enUS'].items():
                    chinese_name = localized['zhCN'].get(record_id, '')
                    if english_name == 'Midnight':
                        continue  # 攻略中是资料片名称，不能自动当成同名首领“午夜”。
                    if not english_name or english_name not in source_text or not re.search(r'[\u3400-\u9fff]', chinese_name) or not re.search(r'(?<![A-Za-z])' + re.escape(english_name) + r'(?![A-Za-z])', source_text):
                        continue
                    _, added = ClassGuideTerm.objects.get_or_create(game_version=version, kind='phrase',
                        object_id=ClassGuideTerm.phrase_identifier(english_name), defaults={'name_en':english_name, 'name_zh':chinese_name,
                            'evidence':'攻略冒险指南 {}；{} ID {}；zhCN SHA256 {}'.format(build, table, record_id, hashlib.sha256(paths['zhCN'].read_bytes()).hexdigest())})
                    created += added
        known_macro_names = macro_name_map(ClassGuideTerm.objects.filter(game_version=version,
            kind__in=['spell', 'item', 'phrase', 'macro']).values_list('kind', 'name_en', 'name_zh'))
        macro_names = {name for code in source_macros for name in localize_macro(code, known_macro_names)[1]}
        macro_keys = {canonical(name) for name in macro_names}
        for old in historical:
            old['macro_names'] = defaultdict(set)
            for spell_id, english_name in old['tables']['enUS'].items():
                key = canonical(english_name)
                chinese_name = old['tables']['zhCN'].get(spell_id, '')
                if key in macro_keys and re.search(r'[\u3400-\u9fff]', chinese_name):
                    old['macro_names'][key].add(chinese_name)
        macro_unresolved = []
        for english_name in sorted(macro_names):
            candidates = names.get(canonical(english_name), [])
            choices = {pair[1] for pair in candidates}
            matched_build, matched_digest = build, digests['zhCN']
            if len(choices) != 1:
                for old in historical:
                    old_choices = old['macro_names'].get(canonical(english_name), set())
                    if len(old_choices) == 1:
                        choices, matched_build, matched_digest = old_choices, old['build'], old['digests']['zhCN']
                        break
            if len(choices) != 1:
                macro_unresolved.append(english_name); continue
            _, added = ClassGuideTerm.objects.get_or_create(game_version=version, kind='phrase',
                object_id=ClassGuideTerm.phrase_identifier(english_name), defaults={'name_en':english_name,'name_zh':next(iter(choices)),
                    'evidence':'攻略宏完整技能名称匹配；客户端 {}；zhCN SHA256 {}'.format(matched_build, matched_digest)})
            created += added
        if macro_unresolved:
            self.stdout.write('宏名称待核对：' + '、'.join(macro_unresolved))
        for key, ref in refs.items():
            english = next(iter(sorted(ref['source_names'])), '')
            chinese, spell_ids = '', []
            if key[0] == 'spell':
                spell_ids = [key[1]]
                chinese = tables['zhCN'].get(key[1], '')
                english = tables['enUS'].get(key[1], english)
            elif key[0] == 'talent':
                if not english and key[1] in hero_entries:
                    english = hero_entries[key[1]]
                    ref['source_names'].add(english)
                candidates = [pair for name in ref['source_names'] for pair in names.get(canonical(name), [])]
                translations = {pair[1] for pair in candidates}
                if len(translations) == 1:
                    chinese = next(iter(translations)); spell_ids = sorted({pair[0] for pair in candidates})
                elif len(ref['source_names']) == 1:
                    chinese = HERO_SUBTREE_NAME_ZH.get(english, '')
                definition = entries.get(key[1], {})
                spell_id = definition.get('SpellID')
                source_spell = game_data.get('spells', {}).get(str(spell_id), {})
                entry_name = definition.get('OverrideName_lang') or source_spell.get('Name_lang') or tables['enUS'].get(spell_id, '')
                forms = {canonical(n) for n in ref['source_names']}
                forms |= {name[:-2] for name in forms if name.endswith("'s")} | {name[:-1] for name in forms if name.endswith('s')}
                if canonical(entry_name) in forms:
                    official = tables['zhCN'].get(spell_id, '')
                    if re.search(r'[\u3400-\u9fff]', official):
                        chinese, english, spell_ids = official, entry_name, [spell_id]
            matched_talents = []
            if not re.search(r'[\u3400-\u9fff]', chinese) and key[0] in ('spell', 'talent'):
                forms = {canonical(name) for name in ref['source_names']}
                # 完整名称加职业限制，避免把同名首领法术误认成玩家技能。
                matched_talents = [row for row in talent_names if row.get('class_id') in ref['classes'] and
                    (canonical(row.get('en')) in forms or (key[0] == 'talent' and canonical(row.get('en')).removesuffix('s') in forms))]
                choices = {row.get('zh', '') for row in matched_talents if re.search(r'[\u3400-\u9fff]', row.get('zh', ''))}
                if len(choices) == 1:
                    chinese = next(iter(choices)); spell_ids = sorted({row['spell_id'] for row in matched_talents})
                    english = next(iter(sorted(ref['source_names'])), english)
            historical_match = None
            if not re.search(r'[\u3400-\u9fff]', chinese) and key[0] in ('spell', 'talent'):
                expected_names = {canonical(name) for name in ref['source_names']} - {''}
                for old in historical:
                    old_spell_id = key[1] if key[0] == 'spell' else old['entries'].get(key[1])
                    old_english = old['tables']['enUS'].get(old_spell_id, '')
                    old_chinese = old['tables']['zhCN'].get(old_spell_id, '')
                    if old_english and (not expected_names or canonical(old_english) in expected_names) and re.search(r'[\u3400-\u9fff]', old_chinese):
                        chinese, english, spell_ids = old_chinese, old_english, [old_spell_id]
                        historical_match = old
                        break
            ref.update(name_en=english, name_zh=chinese, spell_ids=spell_ids)
            icon = ''
            definition = entries.get(key[1], {}) if key[0] == 'talent' else {}
            icon_spell = (spell_ids[0] if len(spell_ids) == 1 else None) or definition.get('SpellID') or (key[1] if key[0] == 'spell' else None)
            icon_source = game_data.get('spells', {}).get(str(icon_spell), {})
            misc = icon_source.get('Misc', [{}])
            icon_id = definition.get('OverrideIcon') or (misc[0].get('SpellIconFileDataID') if misc else 0) or spell_icons.get(icon_spell)
            if icon_id:
                icon = 'https://assets-ng.maxroll.gg/wow/icons/{}.webp'.format(int(icon_id))
            elif key[0] == 'talent':
                icon = hero_icons.get(key[1], '')
            if matched_talents and not icon:
                icon = next((row.get('icon') for row in matched_talents if row.get('icon')), '')
            if historical_match and historical_match['icons'].get(spell_ids[0]):
                icon = 'https://assets-ng.maxroll.gg/wow/icons/{}.webp'.format(historical_match['icons'][spell_ids[0]])
            if re.search(r'[\u3400-\u9fff]', chinese):
                match_evidence = ('；同职业英文天赋名称匹配 {}；数据 SHA256 {}'.format(spell_ids[:5],
                    hashlib.sha256(Path(options['talent_names']).read_bytes()).hexdigest())) if matched_talents else ''
                record_evidence = evidence + (match_evidence or ('；天赋关联技能 {}'.format(spell_ids[:5]) if key[0] == 'talent' else '；按技能 ID 对应'))
                if historical_match:
                    record_evidence = '攻略历史快照 {}；技能 ID {} 及英文名称一致；zhCN SHA256 {}；仅核对原文旧名称，不表示该技能在当前版本仍可用'.format(
                        historical_match['build'], spell_ids[0], historical_match['digests']['zhCN'])
                record, added = ClassGuideTerm.objects.get_or_create(game_version=version, kind=key[0], object_id=key[1],
                    defaults={'name_en': english, 'name_zh': chinese, 'icon': icon,
                        'evidence': record_evidence})
                if not added and icon and not record.icon and record.name_zh == chinese and record.evidence.startswith('攻略客户端快照'):
                    record.icon = icon; record.save(update_fields=['icon'])
                if not added and record.name_zh == chinese and record.name_en != english and record.evidence.startswith('攻略客户端快照'):
                    record.name_en = english; record.save(update_fields=['name_en'])
                created += added
        self.stdout.write('客户端名称新增 {} 条，攻略引用 {} 项'.format(created, len(refs)))
        if not options['fetch_tooltips']:
            return
        cache_dir = Path(options['cache_dir']) / version / str(options['tooltip_env'])
        cache_dir.mkdir(parents=True, exist_ok=True)
        requests_needed = set()
        for key, ref in refs.items():
            if key[0] in ('item', 'spell'):
                requests_needed.add(key)
            elif ref['spell_ids']:
                requests_needed.add(('spell', ref['spell_ids'][0]))
        def fetch(key):
            kind, object_id = key
            path = cache_dir / '{}-{}.json'.format(kind, object_id)
            result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if all(result.get(str(locale), {}).get('name') for locale in (0, 4)):
                return key, result
            url = 'https://nether.wowhead.com/tooltip/{}/{}'.format(kind, object_id)
            for locale in (0, 4):
                if result.get(str(locale), {}).get('name'):
                    continue
                for attempt in range(2):
                    try:
                        response = requests.get(url, params={'locale': locale, 'dataEnv': options['tooltip_env']}, timeout=18)
                        response.raise_for_status(); data = response.json()
                        if data.get('name'):
                            result[str(locale)] = {'name': data['name'], 'icon': data.get('icon', '')}
                        break
                    except (requests.RequestException, ValueError):
                        continue
            if result:
                result['fetched_at'] = timezone.now().isoformat()
                path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return key, result
        fetched = {}
        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            futures = [pool.submit(fetch, key) for key in sorted(requests_needed)]
            for index, future in enumerate(as_completed(futures), 1):
                key, result = future.result(); fetched[key] = result
                if index % 50 == 0:
                    self.stdout.write('中文资料缓存 {}/{}'.format(index, len(futures)))
        counts = Counter()
        for key, ref in refs.items():
            lookup = key if key[0] != 'talent' else ('spell', ref['spell_ids'][0]) if ref['spell_ids'] else None
            payload = fetched.get(lookup, {})
            english = payload.get('0', {}).get('name', '')
            chinese = payload.get('4', {}).get('name', '')
            icon = payload.get('4', {}).get('icon') or payload.get('0', {}).get('icon', '')
            expected = ({canonical(n) for n in ref['source_names']} | {canonical(ref['name_en'])}) - {''}
            if not english or (expected and canonical(english) not in expected) or not re.search(r'[\u3400-\u9fff]', chinese):
                counts['identity_or_locale_missing'] += 1
                continue
            source = '攻略中文 Tooltip {}；环境 {}；{}；英文名称匹配'.format(lookup, options['tooltip_env'], payload.get('fetched_at', ''))
            record, added = ClassGuideTerm.objects.get_or_create(game_version=version, kind=key[0], object_id=key[1],
                defaults={'name_en': english, 'name_zh': chinese, 'icon': icon, 'evidence': source})
            if not added and not record.icon and record.evidence.startswith('攻略客户端快照') and record.name_zh == chinese:
                record.icon = icon; record.save(update_fields=['icon'])
            counts['added' if added else 'existing'] += 1
        self.stdout.write(json.dumps(dict(counts), ensure_ascii=False))
