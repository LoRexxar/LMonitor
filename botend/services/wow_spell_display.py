"""中央技能展示资料：后台一次合成，页面批量只读，不按构建号匹配。"""
from collections import defaultdict
from copy import copy
from datetime import timedelta
import re

from django.db import transaction
from django.utils import timezone

from botend.models import WowSpellSnapshot, WowSpellEffectSnapshot, WowTalentNodeMetadata
from botend.wow.branches import data_branch, spell_branch, spell_branch_aliases
from botend.wow.spell_text import SpellTextResolver
from botend.wow.talents.versioning import TalentVersionResolver


def context_key(class_name='', spec_name='', node_id=None):
    key = '/'.join(re.sub(r'[\s_-]+', '', str(v or '').casefold()) for v in (class_name, spec_name))
    return f'{key}/{node_id}' if node_id else key


def node_spell_id(node):
    """节点编号不是技能编号；只接受明确关联或完整的技能描述引用。"""
    sid = getattr(node, 'display_spell_id', None)
    if sid:
        return sid
    sid = getattr(node, 'spell_id', None)
    if sid and not (getattr(node, 'tree_type', '') == 'hero_anchor' and sid == getattr(node, 'node_id', None)):
        return sid
    text = getattr(node, 'description_zh', '') or getattr(node, 'description', '') or ''
    match = re.fullmatch(r'\s*\$@spelldesc(\d+)\s*', text)
    return int(match[1]) if match else None


def plain_text(value):
    """兼容未回填的普通文本；未解析模板不在请求期间计算或泄漏。"""
    text = ' '.join(str(value or '').split())
    return '' if '$' in text or re.search(r'\?[!()asc\d].*\[', text) else text


class SpellDisplayReader:
    """一个请求内按缺失 ID 批量取中央资料，没有写库和联网操作。"""
    def __init__(self, branch='retail'):
        self.branch = data_branch(branch)
        self.rows = {}
        self.english = {}

    def preload(self, spell_ids):
        ids = {int(sid) for sid in spell_ids if sid and str(sid).isdigit()} - self.rows.keys()
        if not ids:
            return
        self.rows.update(dict.fromkeys(ids))
        for row in WowSpellSnapshot.objects.filter(
                branch=spell_branch(self.branch), locale__in=('zhCN', 'enUS'), spell_id__in=ids):
            if row.locale == 'enUS':
                self.english[row.spell_id] = row
            if row.locale == 'zhCN' or self.rows[row.spell_id] is None:
                self.rows[row.spell_id] = row

    def get(self, spell_id, *, class_name='', spec_name='', node_id=None):
        if not spell_id:
            return {}
        self.preload([spell_id])
        row = self.rows.get(int(spell_id))
        if not row:
            return {}
        data = row.display_data or {}
        contexts = data.get('contexts') or {}
        if data.get('default'):
            result = dict(data['default'])
            for key in (context_key(class_name, spec_name), context_key(class_name, spec_name, node_id)):
                result.update(contexts.get(key) or {})
            return result
        en = self.english.get(int(spell_id))
        return {'name': row.name_zh or row.name, 'name_zh': row.name_zh,
                'name_en': en.name if en else (row.name if not re.search(r'[\u3400-\u9fff]', row.name) else ''),
                'description_zh': plain_text(row.description) if row.locale == 'zhCN' else '',
                'description': plain_text(en.description) if en else '',
                'aura_description_zh': plain_text(row.aura_description) if row.locale == 'zhCN' else '',
                'aura_description': plain_text(en.aura_description) if en else '', 'icon': row.icon}


class CentralTextResolver(SpellTextResolver):
    """历史分支别名只在后台归并，缺失引用仍不得跨正式服和测试服。"""
    def _spell_snapshot(self, spell_id):
        result = super()._spell_snapshot(spell_id)
        checked = self.__dict__.setdefault('_alias_spell_checked', set())
        if any(result.values()) or spell_id in checked:
            return result
        checked.add(spell_id)
        row = WowSpellSnapshot.objects.filter(spell_id=spell_id, locale=self.locale,
            branch__in=spell_branch_aliases(self.branch)).exclude(branch=self.branch).order_by('-updated_at', '-pk').first()
        if row:
            result = {key: getattr(row, key) for key in ('name', 'name_zh', 'description', 'aura_description')}
            self._spell_cache[spell_id] = result
        return result

    def _effects(self, spell_id):
        result = super()._effects(spell_id)
        checked = self.__dict__.setdefault('_alias_effect_checked', set())
        if result or spell_id in checked:
            return result
        checked.add(spell_id)
        rows = list(WowSpellEffectSnapshot.objects.filter(spell_id=spell_id, locale=self.locale,
            branch__in=spell_branch_aliases(self.branch)).exclude(branch=self.branch).order_by('-updated_at', '-pk'))
        if rows:
            selected_branch = rows[0].branch
            result = {row.effect_index: {key: getattr(row, key) for key in
                      ('base_points', 'coefficient', 'pvp_multiplier')} for row in rows if row.branch == selected_branch}
            self._effect_cache[spell_id] = result
        return result


def _unique_projection(projections):
    """共享技能有不同节点语义时只合并一致字段，避免随机取错说明。"""
    fields = set().union(*(row.keys() for row in projections)) if projections else set()
    return {key: next(iter(values)) if len(values) == 1 else '' for key in fields
            if (values := {row.get(key, '') for row in projections if row.get(key, '')})}


@transaction.atomic
def sync_spell_display(*, branch='retail', apply=False, batch_size=200, spell_ids=None):
    """合并当前分支资料；预览不写库，相同结果不更新，重复运行不增加副本。"""
    from botend.wow.talents.metadata import TalentMetadataProvider

    if batch_size < 1:
        raise ValueError('批次大小必须大于零')
    branch = data_branch(branch)
    try:
        version = TalentVersionResolver.resolve(version_key=branch)
    except ValueError:
        version = None
    if apply and version:
        version = type(version).objects.select_for_update().get(pk=version.pk)
    nodes = WowTalentNodeMetadata.objects.filter(talent_version=version) if version else WowTalentNodeMetadata.objects.none()
    by_spell = defaultdict(list)
    for node in nodes:
        sid = node_spell_id(node)
        if sid:
            by_spell[int(sid)].append(node)
    query = WowSpellSnapshot.objects.filter(branch__in=spell_branch_aliases(branch))
    supplements = defaultdict(list)
    for row in WowTalentNodeMetadata.all_objects.filter(localization_only=True, name_kind='spell',
            talent_version__branch=branch, reference_id__isnull=False).order_by('pk'):
        supplements[row.reference_id].append(row)
    ids = set(query.values_list('spell_id', flat=True)) | by_spell.keys() | supplements.keys()
    if spell_ids is not None:
        ids &= set(map(int, spell_ids))
    ids = sorted(ids)
    provider = TalentMetadataProvider(talent_version=version, compile_display=True)
    resolvers = {locale: CentralTextResolver(locale=locale, branch=spell_branch(branch),
                 dump_dir=getattr(version, 'source_dir', '') or None, allow_legacy_dump=False) for locale in ('zhCN', 'enUS')}
    provider._spell_text_resolvers = resolvers
    report = dict(branch=branch, scanned=len(ids), created=0, updated=0, unchanged=0, incomplete=0)
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        sources = defaultdict(dict)
        targets = {}
        rows = list(query.filter(spell_id__in=chunk).order_by('updated_at', 'pk'))
        # 规范来源优先；历史别名只用于一次性归并补缺，不跨业务分支。
        rows.sort(key=lambda row: row.branch == spell_branch(branch))
        for row in rows:
            combined = copy(row)
            previous = sources[row.spell_id].get(row.locale)
            if previous:
                for key in ('name', 'name_zh', 'description', 'aura_description', 'icon'):
                    if not getattr(combined, key):
                        setattr(combined, key, getattr(previous, key))
            sources[row.spell_id][row.locale] = combined
            if row.branch == spell_branch(branch) and row.locale == 'zhCN':
                targets[row.spell_id] = row
        creates, updates = [], []
        for sid in chunk:
            source = sources[sid]
            default = dict.fromkeys(('name', 'name_en', 'name_zh', 'description', 'description_zh',
                                     'aura_description', 'aura_description_zh', 'icon'), '')
            for locale, suffix in (('zhCN', '_zh'), ('enUS', '')):
                raw = source.get(locale)
                if raw:
                    default['name' + suffix] = raw.name_zh or raw.name if suffix else raw.name
                    default['description' + suffix] = resolvers[locale].resolve(raw.description, sid)
                    default['aura_description' + suffix] = resolvers[locale].resolve(raw.aura_description, sid)
                    default['icon'] = default.get('icon') or raw.icon
                    if raw.name and not re.search(r'[\u3400-\u9fff]', raw.name):
                        default['name_en'] = raw.name
            names = supplements[sid]
            current_names = [row for row in names if version and row.talent_version_id == version.pk]
            names = current_names or names
            if names and len({row.name_zh for row in names if row.name_zh}) == 1:
                supplement = next(row for row in names if row.name_zh)
                default['name_zh'] = supplement.name_zh
                default['name_en'] = supplement.name or default['name_en']
                default['icon'] = supplement.icon or default['icon']
            contexts, spec_rows = {}, defaultdict(list)
            for node in by_spell[sid]:
                candidate = copy(node)
                for field, locale in (('description_zh', 'zhCN'), ('description', 'enUS')):
                    raw = source.get(locale)
                    if raw and raw.description and not getattr(candidate, field):
                        setattr(candidate, field, raw.description)
                projected = provider._as_dict(candidate)
                final = {key: projected.get(key, '') for key in
                         ('name', 'name_zh', 'icon', 'description', 'description_zh')}
                final['name_en'] = candidate.name if not re.search(r'[\u3400-\u9fff]', candidate.name) else ''
                for key, value in default.items():
                    if not final.get(key):
                        final[key] = value
                contexts[context_key(node.class_name, node.spec_name, node.node_id)] = final
                spec_rows[context_key(node.class_name, node.spec_name)].append(final)
            for key, values in spec_rows.items():
                contexts[key] = _unique_projection(values)
            shared = _unique_projection(list(contexts.values()))
            for key, value in shared.items():
                if not default.get(key):
                    default[key] = value
            if not re.search(r'[\u3400-\u9fff]', default.get('name_zh') or ''):
                default['name_zh'] = ''
            default['name'] = default.get('name_zh') or default.get('name') or default.get('name_en', '')
            incomplete = any('$' in str(value) for item in [default, *contexts.values()] for value in item.values())
            incomplete = incomplete or not default.get('name_zh') or not any(
                re.search(r'[\u3400-\u9fff]', row.get('description_zh') or '')
                for row in [default, *contexts.values()])
            report['incomplete'] += int(incomplete)
            # 相同字段只存一份。专精保存与公共资料的差值，节点仅保存额外差异。
            compact = {}
            for key, value in contexts.items():
                parent = contexts.get(key.rsplit('/', 1)[0], default) if key.count('/') == 2 else default
                delta = {field: text for field, text in value.items() if parent.get(field, '') != text}
                if delta:
                    compact[key] = delta
            payload = {'default': default, 'contexts': compact,
                       'source': 'wago+talent_supplement', 'branch': branch,
                       'status': 'partial' if incomplete else 'ready'}
            target = targets.get(sid)
            if target is None:
                target = WowSpellSnapshot(branch=spell_branch(branch), locale='zhCN', spell_id=sid,
                                          display_data=payload)
                creates.append(target)
                report['created'] += 1
            elif target.display_data != payload:
                target.display_data = payload
                updates.append(target)
                report['updated'] += 1
            else:
                report['unchanged'] += 1
        if apply:
            WowSpellSnapshot.objects.bulk_create(creates, batch_size=batch_size)
            # 合成不能改变原始资料时间，否则下次会误把合成当成上游更新。
            WowSpellSnapshot.objects.bulk_update(updates, ['display_data'], batch_size=batch_size)
    if apply and branch == 'retail' and (report['created'] or report['updated']):
        from botend.models import MonitorTask
        # 现有后台任务负责原子发布聚合文件，合并命令不直接覆盖页面缓存。
        MonitorTask.objects.filter(name='SpecDetailAggregationMonitor', is_active=True).update(
            last_scan_time=timezone.now() - timedelta(days=365))
    return report
