"""关联冒险手册全量表并原子发布，保存缺项清单和每份来源的版本。"""
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.services.journal_source import WagoJournalSource, latest_retail_build
from botend.services.journal_text import JournalText, grouped, index, integer


ROLE_FLAGS = ((1, 'tank', '坦克'), (2, 'dps', '输出'), (4, 'healer', '治疗'))
ICON_FLAGS = ((8, '高难度机制'), (16, '致命'), (32, '重要'), (64, '可打断'),
              (128, '可驱散魔法'), (256, '可驱散诅咒'), (512, '可驱散毒药'),
              (1024, '可驱散疾病'), (2048, '激怒'))
SLOTS = {0: '其他', 1: '头部', 2: '颈部', 3: '肩部', 4: '衬衣', 5: '胸部', 6: '腰部',
         7: '腿部', 8: '脚部', 9: '腕部', 10: '手部', 11: '手指', 12: '饰品', 13: '单手',
         14: '盾牌', 15: '远程', 16: '背部', 17: '双手', 19: '战袍', 20: '胸部',
         21: '主手', 22: '副手', 23: '副手物品', 25: '投掷', 26: '远程', 28: '圣物'}


def allowed_difficulties(row, relations, available, difficulties):
    if not integer(row.get('Flags')) & 2:
        return available[:]
    explicit = relations.get(integer(row['ID']), [])
    if explicit:
        return [d for d in available if d in {integer(r['DifficultyID']) for r in explicit}]
    mask = integer(row.get('DifficultyMask'), -1) & 255
    return [d for d in available if 0 <= integer(difficulties.get(d, {}).get('OldEnumValue'), -1) < 8
            and mask & (1 << integer(difficulties[d]['OldEnumValue']))]


def section_tree(rows, first_id):
    """沿客户端的首子节/下一同级节构建层级，不按标题猜阶段。"""
    by_id = index(rows)
    visited = set()
    flattened = []

    def walk(section_id, parent=0, depth=0):
        chain = set()
        while section_id:
            if section_id in chain or depth > 50:
                raise ValueError(f'手册技能树存在循环：{section_id}')
            chain.add(section_id)
            if section_id in visited:
                return
            row = by_id.get(section_id)
            if row is None:
                raise ValueError(f'手册技能树缺失节点：{section_id}')
            visited.add(section_id)
            flattened.append((row, parent, depth))
            walk(integer(row.get('FirstChildSectionID')), section_id, depth + 1)
            section_id = integer(row.get('NextSiblingSectionID'))

    if first_id:
        walk(first_id)
    return flattened, sorted(set(by_id) - visited)


def compile_journal(tables, *, item_fallback=None):
    instances = index(tables['JournalInstance'])
    encounters = grouped(tables['JournalEncounter'], 'JournalInstanceID')
    sections = grouped(tables['JournalEncounterSection'], 'JournalEncounterID')
    loot = grouped(tables['JournalEncounterItem'], 'JournalEncounterID')
    creatures = grouped(tables['JournalEncounterCreature'], 'JournalEncounterID')
    tiers = index(tables['JournalTier'])
    tier_links = grouped(tables['JournalTierXInstance'], 'JournalInstanceID')
    maps = index(tables['Map'])
    map_difficulties = grouped(tables['MapDifficulty'], 'MapID')
    difficulties = index(tables['Difficulty'])
    dungeon_encounters = index(tables.get('DungeonEncounter', []))
    section_diff = grouped(tables['JournalSectionXDifficulty'], 'JournalEncounterSectionID')
    encounter_diff = grouped(tables.get('JournalEncounterXDifficulty', []), 'JournalEncounterID')
    item_diff = grouped(tables['JournalItemXDifficulty'], 'JournalEncounterItemID')
    items = index(tables['Item'])
    sparse = index(tables['ItemSparse'])
    resolver = JournalText(tables)
    report = {'instances': 0, 'encounters': 0, 'sections': 0, 'loot': 0, 'missing_item_ids': [],
              'encounters_without_sections': [], 'encounters_without_loot': [], 'unlinked_sections': {},
              'dynamic_sections': [], 'excluded_unassigned_encounters': [], 'missing_difficulties': []}
    report['excluded_internal_encounters'] = []
    report['excluded_unlisted_instances'] = []
    report['instances_without_description'] = []
    report['encounters_without_description'] = []
    report['sections_without_description'] = []
    report['unlocalized_item_ids'] = []
    output = []
    for iid, row in instances.items():
        map_id = integer(row['MapID'])
        map_row = maps.get(map_id, {})
        links = [r for r in tier_links.get(iid, []) if integer(r['JournalTierID']) in tiers]
        if not links:
            report['excluded_unlisted_instances'].append(iid)
            continue
        instance_tiers = sorted({integer(r['JournalTierID']) for r in links})
        historical_tiers = [tiers[t] for t in instance_tiers if integer(tiers[t]['Expansion']) < 9000]
        expansion = max((integer(t['Expansion']) for t in historical_tiers), default=0)
        available = sorted({integer(r['DifficultyID']) for r in map_difficulties.get(map_id, [])
                            if integer(r['DifficultyID']) in difficulties and integer(r['DifficultyID']) > 0
                            and integer(difficulties[integer(r['DifficultyID'])].get('Flags'), 4) & (4 | 16 | 512)},
                           key=lambda d: (integer(difficulties[d]['OrderIndex']), d))
        kind = {1: 'dungeon', 2: 'raid'}.get(integer(map_row.get('InstanceType')), 'world')
        if integer(row.get('Flags')) & 2:
            # 世界首领分组隐藏难度选择，并借用团本地图与普通团队掉落上下文。
            # 有逐首领难度限制的分组仍保留真实难度，例如旧版 10/25 人首领。
            unrestricted = encounters.get(iid) and not any(integer(b.get('Flags')) & 2 for b in encounters[iid])
            if unrestricted:
                kind, available = 'world', [14]
        if not available:
            report['missing_difficulties'].append(iid)
            available = [0]
        boss_rows = []
        for boss in sorted(encounters.get(iid, []), key=lambda r: (integer(r['OrderIndex']), integer(r['ID']))):
            eid = integer(boss['ID'])
            if integer(boss.get('Flags')) & 33:
                report['excluded_internal_encounters'].append(eid)
                continue
            boss_difficulties = allowed_difficulties(boss, encounter_diff, available, difficulties)
            tree, unlinked = section_tree(sections.get(eid, []), integer(boss['FirstSectionID']))
            if unlinked:
                report['unlinked_sections'][str(eid)] = unlinked
            compiled = []
            visible_by_id = {}
            roles_by_id = {}
            for section, parent, depth in tree:
                sid = integer(section['ID'])
                ds = allowed_difficulties(section, section_diff, available, difficulties)
                ds = [d for d in ds if d in boss_difficulties]
                if parent:
                    ds = [d for d in ds if d in visible_by_id.get(parent, [])]
                visible_by_id[sid] = ds
                flags = integer(section['IconFlags'])
                roles = [key for flag, key, _ in ROLE_FLAGS if flags & flag] or roles_by_id.get(parent, [])
                roles_by_id[sid] = roles
                descriptions = {}
                unresolved = {}
                spell_id = integer(section['SpellID'])
                body = section.get('BodyText_lang') or ''
                # 客户端节正文是补充说明；关联技能的主说明来自 Spell 表。
                spell_body = resolver.spells.get(spell_id, {}).get('Description_lang', '')
                source_text = '\n\n'.join(part for part in (spell_body, body) if part)
                if not source_text.strip():
                    report['sections_without_description'].append(sid)
                for difficulty in ds:
                    text, missing = resolver.resolve(source_text, spell_id, difficulty)
                    descriptions[str(difficulty)] = text
                    if missing:
                        unresolved[str(difficulty)] = missing
                if unresolved:
                    report['dynamic_sections'].append(sid)
                source_title = resolver.names.get(spell_id, {}).get('Name_lang') or section.get('Title_lang', '')
                title, _ = resolver.resolve(source_title, spell_id, ds[0] if ds else 0)
                compiled.append({'id': sid, 'parent': parent, 'depth': depth, 'title': title,
                                 'spell_id': integer(section['SpellID']), 'icon': integer(section.get('IconFileDataID')),
                                 'type': integer(section['Type']), 'roles': roles,
                                 'tags': [label for flag, label in ICON_FLAGS if flags & flag],
                                 'difficulty_ids': ds, 'descriptions': descriptions, 'dynamic': unresolved,
                                 'source_text': source_text})
            drops = []
            for drop in loot.get(eid, []):
                if integer(drop['Flags']) & 1:
                    continue
                item_id = integer(drop['ItemID'])
                item = items.get(item_id, {})
                meta = sparse.get(item_id, {})
                fallback = (item_fallback or {}).get(item_id, {})
                name = meta.get('Display_lang') or fallback.get('name')
                if not name:
                    report['missing_item_ids'].append(item_id)
                if not meta.get('Display_lang') and fallback.get('source') == 'wago-enUS':
                    report['unlocalized_item_ids'].append(item_id)
                slot = integer(meta.get('InventoryType', item.get('InventoryType')))
                quality = meta.get('OverallQualityID', fallback.get('quality'))
                drops.append({'id': integer(drop['ID']), 'item_id': item_id, 'name': name or f'物品 {item_id}',
                              'quality': integer(quality) if quality not in (None, '') else None,
                              'icon': integer(item.get('IconFileDataID')), 'slot': slot,
                              'slot_name': SLOTS.get(slot, '其他'), 'class_id': integer(item.get('ClassID')),
                              'subclass_id': integer(item.get('SubclassID')), 'class_mask': integer(meta.get('AllowableClass'), -1),
                              'description': meta.get('Description_lang') or fallback.get('description', ''),
                              'required_level': integer(meta.get('RequiredLevel')),
                              'bonding': integer(meta.get('Bonding')),
                              'stat_types': sorted({integer(meta.get(f'StatModifier_bonusStat_{n}')) for n in range(10)
                                                    if integer(meta.get(f'StatPercentEditor_{n}')) > 0}),
                              'difficulty_ids': [d for d in allowed_difficulties(drop, item_diff, available, difficulties) if d in boss_difficulties],
                              'faction': {-2: 'alliance', -3: 'horde'}.get(integer(drop['FactionMask']), 'both'),
                              'rare': '极其稀有' if integer(drop['Flags']) & 16 else '非常稀有' if integer(drop['Flags']) & 8 else '',
                              'display_season_id': integer(drop.get('DisplaySeasonID')),
                              'condition_id': integer(drop.get('WorldStateExpressionID')),
                              'source': 'wago' if meta.get('Display_lang') else fallback.get('source', 'missing'),
                              'url': f'https://www.wowhead.com/cn/item={item_id}'})
            if not compiled:
                report['encounters_without_sections'].append(eid)
            if not drops:
                report['encounters_without_loot'].append(eid)
            if not boss.get('Description_lang'):
                report['encounters_without_description'].append(eid)
            boss_rows.append({'id': eid, 'name': boss['Name_lang'], 'description': boss.get('Description_lang', ''),
                              'faction': 'alliance' if integer(boss.get('Flags')) & 4 else 'horde' if integer(boss.get('Flags')) & 8 else 'both',
                              'order': integer(boss['OrderIndex']), 'sections': compiled, 'loot': drops,
                              'creatures': [{'name': c['Name_lang'], 'image': integer(c.get('FileDataID'))}
                                            for c in creatures.get(eid, [])],
                              'difficulty_ids': boss_difficulties,
                              'dungeon_encounter_id': integer(boss.get('DungeonEncounterID')),
                              'battle_map_id': integer(dungeon_encounters.get(integer(boss.get('DungeonEncounterID')), {}).get('MapID'))})
            report['sections'] += len(compiled)
            report['loot'] += len(drops)
        if not row.get('Description_lang'):
            report['instances_without_description'].append(iid)
        boss_counts = {str(d): sum(d in b['difficulty_ids'] for b in boss_rows) for d in available}
        represented = [d for d in available if boss_counts[str(d)]]
        if represented:
            available = represented
        output.append({'id': iid, 'name': row['Name_lang'], 'description': row.get('Description_lang', ''),
                       'kind': kind, 'expansion': expansion, 'tier_ids': instance_tiers,
                       'image': integer(row.get('ButtonFileDataID')), 'background': integer(row.get('BackgroundFileDataID')),
                       'map_id': map_id, 'difficulty_ids': available, 'boss_counts': boss_counts, 'encounters': boss_rows})
    report['instances'] = len(output)
    report['encounters'] = sum(len(r['encounters']) for r in output)
    report['missing_item_ids'] = sorted(set(report['missing_item_ids']))
    report['unlocalized_item_ids'] = sorted(set(report['unlocalized_item_ids']))
    report['excluded_unassigned_encounters'] = [integer(e['ID']) for iid, rows in encounters.items() if iid not in instances for e in rows]
    art_ids = {integer(r.get(key)) for r in tables['JournalInstance'] for key in ('ButtonFileDataID', 'BackgroundFileDataID')}
    art_ids.update(integer(r.get('FileDataID')) for r in tables['JournalEncounterCreature'])
    art_ids.update(integer(r.get('IconFileDataID')) for r in tables['JournalEncounterSection'])
    art_ids.update(integer(items.get(integer(r['ItemID']), {}).get('IconFileDataID')) for r in tables['JournalEncounterItem'])
    catalog = {'art_ids': sorted(art_ids - {0}),
               'tiers': [{'id': tid, 'name': r['Name_lang'], 'order': integer(r['Expansion'])} for tid, r in tiers.items()],
               'difficulties': [{'id': did, 'name': r['Name_lang'], 'context': integer(r.get('ItemContext'))}
                                for did, r in difficulties.items()]}
    return output, catalog, report


def sync_journal(*, build='', directory=None, offline=False, refresh=False, progress=None, fallback=True):
    token = str(uuid4())
    now = timezone.now()
    state, _ = JournalState.objects.get_or_create(key='wow-zhCN')
    with transaction.atomic():
        state = JournalState.objects.select_for_update().get(pk=state.pk)
        if state.sync_until and state.sync_until > now:
            raise ValueError('冒险手册已有同步正在运行')
        state.sync_token, state.sync_until = token, now + timedelta(hours=3)
        state.save(update_fields=['sync_token', 'sync_until'])
    release = None
    try:
        build = build or latest_retail_build()
        release = JournalRelease.objects.create(build=build)
        source = WagoJournalSource(build, directory or Path(settings.BASE_DIR) / '.cache' / 'adventure-journal',
                                   offline=offline, refresh=refresh, progress=progress)
        tables = source.load()
        from botend.services.journal_items import supplement_items
        supplements = supplement_items(tables, source, enabled=fallback)
        rows, catalog, report = compile_journal(tables, item_fallback=supplements)
        if not report['instances'] or not report['encounters'] or not report['loot']:
            raise ValueError('核心冒险手册数据为空，拒绝发布')
        if report['missing_item_ids']:
            raise ValueError(f"仍有 {len(report['missing_item_ids'])} 件掉落缺少物品名称，拒绝覆盖完整快照")
        with transaction.atomic():
            state = JournalState.objects.select_for_update().get(pk='wow-zhCN')
            if state.sync_token != token:
                raise ValueError('同步租约已被其他任务接管，拒绝发布过期结果')
            previous = state.active_release
            # 抓取可能超过 lease；必须在最终发布锁内读取最新活动 release，
            # 否则期间合法写入的 PTR overlay 会被旧快照覆盖。
            from botend.services.ptr_journal_gear_overlay import preserve_active_ptr_journal_overlay
            rows, catalog, report, ptr_overlays, published_build = preserve_active_ptr_journal_overlay(
                previous,
                rows,
                catalog,
                report,
                build,
            )
            release.build = published_build
            release.manifest = {
                'tables': source.manifest,
                'catalog': catalog,
                'retail_build': build,
                'ptr_overlays': ptr_overlays,
            }
            release.report = report
            for key, label in (('instances', '副本'), ('encounters', '首领'), ('sections', '技能'), ('loot', '掉落')):
                if previous and report[key] < previous.report.get(key, 0) * .8:
                    raise ValueError(f'{label}数量异常下降超过 20%，拒绝自动发布')
            for row in rows:
                bosses = row.pop('encounters')
                instance = JournalInstance.objects.create(release=release, journal_id=row['id'], name=row['name'],
                                                          kind=row['kind'], expansion=row['expansion'], payload=row)
                JournalEncounter.objects.bulk_create([JournalEncounter(instance=instance, journal_id=b['id'],
                    name=b['name'], order=b['order'], payload=b) for b in bosses])
            release.status = 'completed'
            release.completed_at = timezone.now()
            release.save()
            state.active_release = release
            state.save(update_fields=['active_release'])
        return release
    except Exception as exc:
        if release:
            release.status, release.error, release.completed_at = 'failed', str(exc)[:4000], timezone.now()
            release.save()
        raise
    finally:
        JournalState.objects.filter(pk='wow-zhCN', sync_token=token).update(sync_token='', sync_until=None)
