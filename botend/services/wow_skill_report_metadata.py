"""按报告版本补齐技能图标和调整光环的实际作用对象，不改写历史快照。"""

import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from django.core.cache import cache
from django.db.models import Q

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata
from botend.templatetags.wow_tags import wow_icon_oss_url


def wowhead_spell_url(branch, spell_id):
    prefix = {'wowt': 'ptr/', 'wowxptr': 'ptr-2/', 'wow_beta': 'beta/'}.get(branch, '')
    return f'https://www.wowhead.com/{prefix}spell={int(spell_id)}'


def _build_parts(value):
    return tuple(int(x) for x in re.findall(r'\d+', str(value or '')))


def _compatible_build(candidate, target):
    old, new = _build_parts(candidate), _build_parts(target)
    return len(old) == 4 and len(new) == 4 and old[:2] == new[:2] and old <= new


def database_spell_metadata(spell_ids, branch, build):
    """优先同分支图标，兼容版本的同 ID 图标无冲突时跨分支复用。"""
    ids = set(spell_ids)
    result = {sid: {'icon': '', 'name': '', 'icon_source': ''} for sid in ids}
    rows = list(WowSpellSnapshot.objects.filter(spell_id__in=ids).values(
        'spell_id', 'branch', 'locale', 'snapshot_build', 'name', 'name_zh', 'icon',
    ))
    rows.sort(key=lambda r: (r['snapshot_build'] == build, r['locale'] == 'zhCN', _build_parts(r['snapshot_build'])), reverse=True)
    shared_icons = {sid: set() for sid in ids}
    for row in rows:
        value = result[row['spell_id']]
        if row['branch'] == branch and row['snapshot_build'] == build and not value['name']:
            value['name'] = row['name_zh'] or (row['name'] if row['locale'] == 'zhCN' else '')
        if _compatible_build(row['snapshot_build'], build) and row['icon']:
            shared_icons[row['spell_id']].add(row['icon'])
            if row['branch'] == branch and not value['icon']:
                value.update(icon=row['icon'], icon_source='spell_snapshot')
    talent_branch = {'wow': 'retail', 'wowt': 'ptr', 'wowxptr': 'ptr', 'wow_beta': 'beta'}.get(branch, branch)
    talents = list(WowTalentNodeMetadata.objects.filter(
        Q(spell_id__in=ids) | Q(display_spell_id__in=ids),
    ).values('spell_id', 'display_spell_id', 'name', 'name_zh', 'icon', 'talent_version__current_build', 'talent_version__branch'))
    talents.sort(key=lambda r: _build_parts(r['talent_version__current_build']), reverse=True)
    for row in talents:
        sid = row['display_spell_id'] or row['spell_id']
        version = row['talent_version__current_build']
        if sid not in ids or not _compatible_build(version, build):
            continue
        value = result[sid]
        if row['icon']:
            shared_icons[sid].add(row['icon'])
        same_branch = row['talent_version__branch'] == talent_branch
        if same_branch and not value['icon'] and row['icon']:
            value.update(icon=row['icon'], icon_source='talent_metadata')
        if same_branch and version == build and not value['name']:
            value['name'] = row['name_zh'] or row['name']
    # 上线后的天赋版本可能由 PTR 改标正式服。仅图标允许复用同 ID 的无歧义结果，
    # 不跨分支借用名称、描述或作用关系；不同来源图标有冲突时交给对应分支备用源。
    for sid, icons in shared_icons.items():
        if not result[sid]['icon'] and len(icons) == 1:
            result[sid].update(icon=next(iter(icons)), icon_source='database_shared')
    return result


def _db2_rows(table, build, field, value, locale='enUS'):
    key = f'skill-report-db2-v2:{table}:{build}:{field}:{value}:{locale}'
    found = cache.get(key)
    if found is not None:
        return found
    try:
        for attempt in range(2):
            response = requests.get(f'https://wago.tools/db2/{table}/csv', params={
                'build': build, 'locale': locale, f'filter[{field}]': str(int(value)),
            }, timeout=(4, 12))
            response.raise_for_status()
            # Wago 偶尔返回 200 空正文；重试一次，不能将其视为确定不存在。
            if response.text.strip():
                break
        # 上游筛选失效时仍按准确 ID 过滤，不接受包含匹配或错误 HTML。
        rows = [r for r in csv.DictReader(io.StringIO(response.text)) if r.get(field) == str(value)]
        cache.set(key, rows, 86400 if rows else 120)
        return rows
    except (requests.RequestException, ValueError, csv.Error):
        cache.set(key, [], 120)
        return []


def _parallel(items, fn):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(items, pool.map(fn, items)))


def _tooltip_icon(branch, sid):
    prefix = {'wowt': 'ptr', 'wowxptr': 'ptr-2', 'wow_beta': 'beta'}.get(branch, 'live')
    key = f'skill-report-icon-v1:{prefix}:{sid}'
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.get(f'https://nether.wowhead.com/{prefix}/tooltip/spell/{sid}', timeout=(4, 10))
        response.raise_for_status()
        icon = str(response.json().get('icon') or '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', icon):
            icon = ''
        cache.set(key, icon, 86400 if icon else 120)
        return icon
    except (requests.RequestException, ValueError):
        cache.set(key, '', 120)
        return ''


def report_spell_entries(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    entries = {}
    for node in soup.select('.spell[id^="spell-"]')[:300]:
        raw_id = node.get('id', '').removeprefix('spell-')
        if not raw_id.isdigit():
            continue
        title = node.select_one('.spell-title')
        # 只解析报告明确变更的效果，不能把该光环的所有效果都算作本次改动。
        indices = set()
        for detail in node.select('.impact-evidence, .line'):
            indices.update(int(x) for x in re.findall(r'\(#(\d+)\)', detail.get_text()))
        entries[int(raw_id)] = {'name': title.get_text(strip=True) if title else '', 'indices': indices}
    return entries


def build_report_spell_metadata(html_text, branch, build):
    entries = report_spell_entries(html_text)
    if not entries or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', build):
        return {}
    ids = list(entries)
    # 远程关系按不可变 build 缓存，数据库图标每次重读，补齐后无需重跑报告。
    candidates = [sid for sid in ids if entries[sid]['indices']]
    effects = _parallel(candidates, lambda sid: _db2_rows('SpellEffect', build, 'SpellID', sid)) if candidates else {}
    relations = {}
    for sid, rows in effects.items():
        for row in rows:
            try:
                idx, aura = int(row['EffectIndex']), int(row['EffectAura'])
                label = int(row.get('EffectMiscValue_1') or 0)
            except (KeyError, ValueError):
                continue
            if idx not in entries[sid]['indices']:
                continue
            if aura in (648, 649) and label > 0:
                relations.setdefault(sid, []).append({'index': idx, 'aura': aura, 'label': label, 'relation': 'label'})
            elif aura in (646, 647):
                mask = [int(row.get(f'EffectSpellClassMask_{i}') or 0) for i in range(4)]
                if any(mask):
                    relations.setdefault(sid, []).append({'index': idx, 'aura': aura, 'mask': mask, 'relation': 'class_mask'})
    labels = sorted({r['label'] for rows in relations.values() for r in rows if r['relation'] == 'label'})
    label_rows = _parallel(labels, lambda label: _db2_rows('SpellLabel', build, 'LabelID', label)) if labels else {}
    masked_ids = [sid for sid, rows in relations.items() if any(r['relation'] == 'class_mask' for r in rows)]
    options = _parallel(masked_ids, lambda sid: _db2_rows('SpellClassOptions', build, 'SpellID', sid)) if masked_ids else {}
    class_sets = {sid: int(rows[0].get('SpellClassSet') or 0) for sid, rows in options.items() if rows}
    families = sorted(set(class_sets.values()) - {0})
    family_rows = _parallel(families, lambda family: _db2_rows('SpellClassOptions', build, 'SpellClassSet', family)) if families else {}
    target_ids = set()
    for sid, rows in relations.items():
        for relation in rows:
            if relation['relation'] == 'label':
                matches = label_rows[relation['label']]
            else:
                matches = [row for row in family_rows.get(class_sets.get(sid), []) if any(
                    int(row.get(f'SpellClassMask_{i}') or 0) & relation['mask'][i] for i in range(4)
                )]
            relation['spell_ids'] = sorted({int(r['SpellID']) for r in matches if str(r.get('SpellID', '')).isdigit()})[:50]
            target_ids.update(relation['spell_ids'])
    metadata = database_spell_metadata(set(ids) | target_ids, branch, build)
    missing_names = sorted(sid for sid in target_ids if not metadata[sid]['name'])
    names = _parallel(missing_names, lambda sid: _db2_rows('SpellName', build, 'ID', sid, 'zhCN')) if missing_names else {}
    for sid, rows in names.items():
        metadata[sid]['name'] = next((r.get('Name_lang', '') for r in rows), '')
    # 调整光环使用受影响技能各自的图标，不借用其上游可能存在的占位图标。
    icon_ids = sorted((set(ids) - set(relations)) | target_ids)
    missing_icons = [sid for sid in icon_ids if not metadata[sid]['icon']]
    icons = _parallel(missing_icons, lambda sid: _tooltip_icon(branch, sid)) if missing_icons else {}
    for sid, icon in icons.items():
        if icon:
            metadata[sid].update(icon=icon, icon_source='wowhead')

    def item(sid):
        value = metadata[sid]
        icon = value['icon']
        # 数据库图标优先 OSS，未入库的备用源直接使用原站资源，不拼出不存在的 OSS 对象。
        source = value['icon_source']
        basename = re.sub(r'\.(?:jpg|png|blp)$', '', str(icon).rsplit('/', 1)[-1], flags=re.I)
        fallback = f'https://wow.zamimg.com/images/wow/icons/large/{basename}.jpg' if re.fullmatch(r'[a-zA-Z0-9_-]+', basename) else ''
        return {'id': sid, 'name': value['name'] or entries.get(sid, {}).get('name') or f'技能 #{sid}',
                'url': wowhead_spell_url(branch, sid), 'icon_source': source,
                'icon_fallback_url': fallback,
                'icon_url': (f'https://wow.zamimg.com/images/wow/icons/large/{icon}.jpg' if source == 'wowhead' else wow_icon_oss_url(icon)) if icon else ''}

    result = {}
    for sid in ids:
        result[str(sid)] = {**item(sid), 'effects': [
            {**r, 'targets': [item(target) for target in r['spell_ids']]} for r in relations.get(sid, [])
        ]}
    return result
