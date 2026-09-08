"""全站名称读写：复用天赋节点、法术和物品原表，不保存第二份覆盖表。"""
import hashlib
import re
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

FIELDS = ('game_version', 'kind', 'object_id', 'name_en', 'name_zh', 'icon', 'evidence')


def phrase_identifier(name):
    return int(hashlib.sha256(str(name).strip().casefold().encode()).hexdigest()[:13], 16)


def version_for(value, registry=apps, using='default', create=False):
    model = registry.get_model('botend', 'WowTalentVersion')
    query = model._base_manager.using(using)
    version = query.filter(key=value).first() or query.filter(
        Q(major_version=value) | Q(major_version=value + '.0') |
        Q(key__endswith='-' + value) | Q(key__endswith='-' + value + '.0')
    ).order_by('-is_active', '-id').first()
    if version is None and create:
        version = query.create(key=value, label=value, major_version=value, status='draft')
    return version


def version_label(version, fallback):
    label = (version.major_version if version else '') or fallback
    return label[:-2] if re.fullmatch(r'\d+\.\d+\.0', label) else label


def validate_name(data):
    row = {key: data.get(key, '') for key in FIELDS}
    for key, limit in [('game_version', 64), ('name_en', 255), ('name_zh', 255), ('icon', 255), ('evidence', 1000)]:
        if not isinstance(row[key], str) or len(row[key]) > limit:
            raise ValidationError('名称资料字段无效：' + key)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', row['game_version']):
        raise ValidationError('必须填写有效的游戏版本')
    if row['kind'] not in ('spell', 'talent', 'item', 'phrase', 'macro') or not re.search(r'[\u3400-\u9fff]', row['name_zh']):
        raise ValidationError('名称类型或中文无效')
    if row['kind'] in ('phrase', 'macro'):
        if not row['name_en'].strip():
            raise ValidationError('专有名词必须填写英文原名')
        row['object_id'] = phrase_identifier(row['name_en'])
    try:
        if isinstance(row['object_id'], bool) or str(int(row['object_id'])) != str(row['object_id']) or int(row['object_id']) < 1:
            raise ValueError
        row['object_id'] = int(row['object_id'])
        if row['object_id'] >= 2**53:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError('名称引用编号无效')
    return row


def _record(obj, kind, version, object_id):
    return dict(id=f'{obj._meta.model_name}:{obj.pk}', game_version=version, kind=kind, object_id=object_id,
        name_en=obj.name, name_zh=obj.name_zh, icon=obj.icon, evidence=obj.localization_evidence,
        model=obj._meta.object_name, pk=obj.pk, supplemental=obj.localization_only,
        aliases=obj.reference_aliases, class_name=obj.class_name, spec_name=obj.spec_name,
        spell_id=obj.spell_id, display_spell_id=obj.display_spell_id, node_id=obj.node_id)


def names_for(game_version, class_name='', spec_name='', registry=apps, using='default', *, reference_ids=None, source_text=None):
    """所有类型同表读取；名称资料与真实节点按标志区分。"""
    version = version_for(game_version, registry, using)
    if not version:
        return []
    game_version = version_label(version, game_version)
    query = registry.get_model('botend', 'WowTalentNodeMetadata')._base_manager.using(using).filter(talent_version=version).exclude(name_zh='')
    if class_name:
        query = query.filter(Q(localization_only=True) | Q(class_name__iexact=class_name, spec_name__iexact=spec_name))
    if source_text is not None:
        from botend.services.wow_news_glossary_service import _extract_name_candidates
        candidates = _extract_name_candidates(source_text)
        if reference_ids is None:
            refs = re.findall(r'\[\[(spell|talent|item):(\d+)', source_text)
            reference_ids = {kind: {int(identity) for typ, identity in refs if typ == kind} for kind in ('spell', 'talent', 'item')}
        selection = Q(name_kind__in=['talent', 'phrase', 'macro']) | Q(name__in=candidates)
        for kind in ('spell', 'item'):
            selection |= Q(name_kind=kind, reference_id__in=reference_ids.get(kind, set()))
        query = query.filter(selection)
    elif reference_ids is not None:
        selection = Q(name_kind='talent')
        for kind in ('spell', 'item'):
            selection |= Q(name_kind=kind, reference_id__in=reference_ids.get(kind, set()))
        query = query.filter(selection)
    records = []
    for obj in query.only('id','name','name_zh','icon','localization_evidence','localization_only','name_kind',
            'reference_id','reference_aliases','talent_id','node_id','spell_id','display_spell_id','class_name','spec_name').order_by('localization_only', 'id'):
        identity = obj.reference_id if obj.localization_only else obj.talent_id or obj.node_id or obj.spell_id
        if identity:
            records.append(_record(obj, obj.name_kind, game_version, identity))
    return records


def effective_names(game_version, **kwargs):
    """导出和翻译保留来源编号，但不在数据库复制对应名称。"""
    result = {}
    for row in names_for(game_version, **kwargs):
        result.setdefault((row['kind'], row['object_id']), row)
        if row['kind'] == 'talent':
            for alias in row['aliases']:
                result.setdefault(('talent', alias), {**row, 'object_id':alias})
    return list(result.values())


def _source_values(row, version, registry, using):
    """迁移/初始化时利用已存在的原始快照，只补齐名称，不建立第二个维护入口。"""
    if row['kind'] == 'spell':
        query = registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(
            spell_id=row['object_id'], branch__in=['wowxptr', 'wowt'] if version.branch == 'ptr' else ['wow']).filter(
                Q(snapshot_build=row['game_version']) | Q(snapshot_build__startswith=row['game_version'] + '.'))
        source = next((r for r in query.exclude(name_zh='').order_by('locale') if re.search(r'[\u3400-\u9fff]', r.name_zh)), None)
    elif row['kind'] == 'item':
        source = registry.get_model('botend', 'WowItemSnapshot')._base_manager.using(using).filter(item_id=row['object_id']).exclude(name_zh='').first()
    else:
        source = None
    if source and re.search(r'[\u3400-\u9fff]', source.name_zh):
        return dict(name_en=source.name or row['name_en'], name_zh=source.name_zh,
                    icon=source.icon or row['icon'], evidence=row['evidence'])
    return row


def write_name(data, *, overwrite=False, registry=apps, using='default'):
    """在既有天赋名称表按类型写入；导入只补缺，手工编辑更新同一记录。"""
    row = validate_name(data)
    with transaction.atomic(using=using):
        version = version_for(row['game_version'], registry, using, create=True)
        row['game_version'] = version_label(version, row['game_version'])
        registry.get_model('botend', 'WowTalentVersion')._base_manager.using(using).select_for_update().get(pk=version.pk)
        kind, identity = row['kind'], row['object_id']
        model = registry.get_model('botend', 'WowTalentNodeMetadata')
        query = model._base_manager.using(using).filter(talent_version=version, name_kind=kind)
        native = []
        if kind == 'talent':
            native = list(query.filter(localization_only=False).filter(Q(talent_id=identity) | Q(node_id=identity)))
            if not native:
                native = [obj for obj in query.filter(localization_only=False) if identity in obj.reference_aliases]
            if not native and row['name_en']:
                native = list(query.filter(localization_only=False, name__iexact=row['name_en']))
            if len({obj.name_zh for obj in native if obj.name_zh}) > 1:
                native = []
        targets = native or list(query.filter(localization_only=True, reference_id=identity))
        if not targets:
            values = row if overwrite else _source_values(row, version, registry, using)
            obj = model._base_manager.using(using).create(talent_version=version, name_kind=kind,
                localization_only=True, reference_id=identity, tree_type='', name=values['name_en'], name_zh=values['name_zh'],
                icon=values['icon'], localization_evidence=values['evidence'])
            return _record(obj, kind, row['game_version'], identity), True
        for obj in targets:
            changed = []
            for field, value in [('name', row['name_en']), ('name_zh', row['name_zh']), ('icon', row['icon']), ('localization_evidence', row['evidence'])]:
                if (overwrite or not getattr(obj, field)) and getattr(obj, field) != value:
                    setattr(obj, field, value); changed.append(field)
            if kind == 'talent' and not obj.localization_only and identity not in obj.reference_aliases:
                obj.reference_aliases = [*obj.reference_aliases, identity]; changed.append('reference_aliases')
            if changed:
                obj.save(using=using, update_fields=changed)
        return _record(targets[0], kind, row['game_version'], identity), False


def export_names(versions):
    # 历史快照可能用英文占位，不能把它作为中文名称放入初始化包。
    return [{key: row[key] for key in FIELDS} for version in sorted(versions) for row in effective_names(version)
            if re.search(r'[\u3400-\u9fff]', row['name_zh'])]
