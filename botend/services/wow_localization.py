"""全站名称读写：复用天赋节点、法术和物品原表，不保存第二份覆盖表。"""
import hashlib
import re
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.db.models import Q

FIELDS = ('game_version', 'kind', 'object_id', 'name_en', 'name_zh', 'icon', 'evidence')
EDIT_STATE_FIELDS = ('name_en', 'name_zh', 'icon', 'evidence')


class NameEditConflict(Exception):
    """The editable source rows changed after the management list was loaded."""


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


def _reference_identity(value):
    if isinstance(value, bool):
        raise ValidationError('名称引用编号无效')
    if isinstance(value, int):
        identity = value
    elif isinstance(value, str) and re.fullmatch(r'[1-9]\d*', value):
        identity = int(value)
    else:
        raise ValidationError('名称引用编号无效')
    if identity < 1 or identity >= 2**53:
        raise ValidationError('名称引用编号无效')
    return identity


def _version_matches_label(version, label):
    if not isinstance(label, str) or not label:
        return False
    normalized = label[:-2] if re.fullmatch(r'\d+\.\d+\.0', label) else label
    major = (version.major_version or '').removesuffix('.0')
    return version.key == label or version.key.endswith('-' + label) or major == normalized


def _preferred_version(labels, concrete_versions, *, registry, using):
    """选择具体版本对象；不接受请求提示，也不把已知分支压缩后再反查。"""
    version_model = registry.get_model('botend', 'WowTalentVersion')
    all_versions = list(version_model._base_manager.using(using).all())
    candidates = {version.pk: version for version in concrete_versions if version}
    for label in dict.fromkeys(labels):
        for version in all_versions:
            if _version_matches_label(version, label):
                candidates.setdefault(version.pk, version)
    pool = list(candidates.values()) or all_versions
    if not pool:
        raise ValidationError('没有可用于名称资料的游戏版本')
    return max(pool, key=lambda value: (
        bool(value.is_default_player_tree), bool(value.is_active), value.pk,
    ))


def _spell_snapshot_branches(version):
    return ['wowxptr', 'wowt'] if version.branch == 'ptr' else ['wow']


def _inferred_reference(identity, *, registry, using):
    """无攻略引用时仅根据现有权威表推断；多类型命中必须拒绝猜测。"""
    metadata_model = registry.get_model('botend', 'WowTalentNodeMetadata')
    metadata = metadata_model._base_manager.using(using).select_related('talent_version')
    existing = list(metadata.filter(localization_only=True, reference_id=identity))
    talent_rows = list(metadata.filter(localization_only=False).filter(
        Q(talent_id=identity) | Q(node_id=identity)))
    if not talent_rows:
        aliases = metadata.filter(localization_only=False)
        if connections[using].features.supports_json_field_contains:
            talent_rows = list(aliases.filter(reference_aliases__contains=[identity]))
        else:
            talent_rows = [obj for obj in aliases if identity in obj.reference_aliases]
    spell_rows = list(metadata.filter(localization_only=False).filter(
        Q(spell_id=identity) | Q(display_spell_id=identity)))
    raw_spell_snapshots = list(
        registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(spell_id=identity))
    version_model = registry.get_model('botend', 'WowTalentVersion')
    snapshot_versions = []
    valid_spell_snapshots = []
    for version in version_model._base_manager.using(using).exclude(current_build=''):
        matches = [snapshot for snapshot in raw_spell_snapshots
                   if snapshot.locale == 'enUS'
                   and snapshot.snapshot_build == version.current_build
                   and snapshot.branch in _spell_snapshot_branches(version)]
        if matches:
            snapshot_versions.append(version)
            valid_spell_snapshots.extend(matches)
    kinds = {obj.name_kind for obj in existing}
    if talent_rows:
        kinds.add('talent')
    if spell_rows or valid_spell_snapshots:
        kinds.add('spell')
    if registry.get_model('botend', 'WowItemSnapshot')._base_manager.using(using).filter(
            item_id=identity).exists():
        kinds.add('item')
    if len(kinds) != 1:
        if not kinds:
            raise ValidationError('该编号未出现在攻略或权威快照中，无法自动识别类型')
        raise ValidationError('该编号匹配多个对象类型，无法安全自动识别')
    versions = [
        obj.talent_version for obj in [*existing, *talent_rows, *spell_rows]
        if obj.talent_version
    ]
    versions.extend(snapshot_versions)
    return kinds.pop(), versions


def _generated_reference_metadata(kind, identity, version, guide_label, *, registry, using):
    name_en = icon = ''
    sources = []
    metadata_model = registry.get_model('botend', 'WowTalentNodeMetadata')
    native = metadata_model._base_manager.using(using).filter(localization_only=False)
    if version:
        native = native.filter(talent_version=version)
    if kind == 'talent':
        native = list(native.filter(Q(talent_id=identity) | Q(node_id=identity)))
        if not native:
            aliases = metadata_model._base_manager.using(using).filter(
                localization_only=False, talent_version=version)
            if connections[using].features.supports_json_field_contains:
                native = list(aliases.filter(reference_aliases__contains=[identity]))
            else:
                native = [obj for obj in aliases if identity in obj.reference_aliases]
    elif kind == 'spell':
        native = list(native.filter(Q(spell_id=identity) | Q(display_spell_id=identity)))
    else:
        native = []
    native_names = {obj.name for obj in native if obj.name}
    native_icons = {obj.icon for obj in native if obj.icon}
    if len(native_names) > 1 or len(native_icons) > 1:
        raise ValidationError('该编号对应多条不同的原生名称资料，无法安全自动补全')
    native_source = next((obj for obj in native if obj.name or obj.icon), None)
    if native_source:
        name_en, icon = native_source.name, native_source.icon
        sources.append('WowTalentNodeMetadata')

    if kind == 'spell' and version.current_build:
        branches = _spell_snapshot_branches(version)
        source = registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(
            spell_id=identity,
            branch__in=branches,
            locale='enUS',
            snapshot_build=version.current_build,
        ).order_by('branch').first()
        if source and (source.name or source.icon):
            name_en = source.name or name_en
            icon = source.icon or icon
            sources.append('WowSpellSnapshot')
    elif kind == 'item':
        source = registry.get_model('botend', 'WowItemSnapshot')._base_manager.using(using).filter(
            item_id=identity).first()
        if source:
            name_en = source.name or name_en
            icon = source.icon or icon
            sources.append('WowItemSnapshot')
    name_en = name_en or guide_label
    return name_en, icon, list(dict.fromkeys(sources))


def normalize_guide_reference(data, *, registry=apps, using='default'):
    """新增时只接收 ID 和中文名；身份及展示元数据均由后端权威事实生成。"""
    row = dict(data)
    identity = _reference_identity(row.get('object_id'))
    name_zh = row.get('name_zh', '')
    if not isinstance(name_zh, str) or len(name_zh) > 255 or not re.search(r'[\u3400-\u9fff]', name_zh):
        raise ValidationError('中文名称无效')

    from botend.guide_models import ClassGuide
    token = re.compile(rf'\[\[(spell|talent|item):{identity}(?:\|([^\]]+))?\]\]')
    references = []
    guides = ClassGuide._base_manager.using(using).filter(
        archived=False, content_markdown__contains=f':{identity}'
    ).values_list('game_version', 'content_markdown')
    for guide_version, content in guides:
        references.extend((guide_version, match.group(1), (match.group(2) or '').strip())
                          for match in token.finditer(content))
    kinds = {kind for _, kind, _ in references}
    if len(kinds) > 1:
        raise ValidationError('该编号在攻略中对应多种引用类型，无法安全自动识别')
    if references:
        kind = kinds.pop()
        version_labels = [guide_version for guide_version, _, _ in references]
        concrete_versions = []
    else:
        kind, concrete_versions = _inferred_reference(identity, registry=registry, using=using)
        version_labels = []
    version = _preferred_version(
        version_labels, concrete_versions, registry=registry, using=using)
    guide_label = next((label for guide_version, reference_kind, label in references
                        if _version_matches_label(version, guide_version)
                        and reference_kind == kind and label), '')
    name_en, icon, sources = _generated_reference_metadata(
        kind, identity, version, guide_label, registry=registry, using=using)
    evidence = f'系统自动识别：攻略显式引用 [[{kind}:{identity}]]' if references else (
        f'系统自动识别：权威快照中的 {kind}:{identity}')
    if sources:
        evidence += '；英文名/图标来源 ' + '、'.join(sources)
    return validate_name(dict(
        game_version=version.key,
        kind=kind,
        object_id=identity,
        name_en=name_en,
        name_zh=name_zh,
        icon=icon,
        evidence=evidence,
    ))


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
    has_reference_filter = reference_ids is not None
    if reference_ids is None and source_text is not None:
        refs = re.findall(r'\[\[(spell|talent|item):(\d+)', source_text)
        reference_ids = {kind: {int(identity) for typ, identity in refs if typ == kind} for kind in ('spell', 'talent', 'item')}
    reference_ids = reference_ids or {}
    talent_reference_ids = {int(value) for value in reference_ids.get('talent', set())}
    spell_reference_ids = {int(value) for value in reference_ids.get('spell', set())}
    if class_name:
        context = Q(localization_only=True) | Q(class_name__iexact=class_name, spec_name__iexact=spec_name)
        if spell_reference_ids:
            # 技能引用也可以命中其他职业/专精天赋节点所承载的 spell；显式 ID 不受攻略上下文限制。
            context |= Q(name_kind='talent') & (
                Q(spell_id__in=spell_reference_ids) | Q(display_spell_id__in=spell_reference_ids)
            )
        if talent_reference_ids:
            # 攻略正文可以明确引用其他专精/职业的天赋；显式 ID 必须按身份解析，不能被当前攻略专精过滤掉。
            direct_rows = query.filter(name_kind='talent').filter(
                Q(talent_id__in=talent_reference_ids) | Q(node_id__in=talent_reference_ids)
            ).values_list('talent_id', 'node_id')
            direct_ids = {int(value) for pair in direct_rows for value in pair if value}
            unmatched_ids = talent_reference_ids - direct_ids
            alias_pks = []
            if unmatched_ids:
                if connections[using].features.supports_json_field_contains:
                    alias_selection = Q()
                    for value in unmatched_ids:
                        alias_selection |= Q(reference_aliases__contains=[value])
                    alias_pks = query.filter(name_kind='talent').filter(alias_selection).values('pk')
                else:
                    # SQLite 测试后端不支持 JSON contains；仅测试/开发环境做 Python 匹配。
                    alias_pks = [
                        pk for pk, aliases in query.filter(name_kind='talent').values_list('pk', 'reference_aliases')
                        if unmatched_ids.intersection(int(value) for value in (aliases or []))
                    ]
            context |= Q(name_kind='talent') & (
                Q(talent_id__in=talent_reference_ids) | Q(node_id__in=talent_reference_ids) | Q(pk__in=alias_pks)
            )
        query = query.filter(context)
    if source_text is not None:
        from botend.services.wow_news_glossary_service import _extract_name_candidates
        candidates = _extract_name_candidates(source_text)
        selection = Q(name_kind__in=['talent', 'phrase', 'macro']) | Q(name__in=candidates)
        for kind in ('spell', 'item'):
            selection |= Q(name_kind=kind, reference_id__in=reference_ids.get(kind, set()))
        query = query.filter(selection)
    elif has_reference_filter:
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


def write_name(data, *, overwrite=False, preserve_blank=False, registry=apps, using='default', target_pk=None, target_state=None):
    """在既有天赋名称表按类型写入；导入只补缺，手工编辑更新同一记录。"""
    row = validate_name(data)
    with transaction.atomic(using=using):
        version = version_for(row['game_version'], registry, using, create=True)
        row['game_version'] = version_label(version, row['game_version'])
        registry.get_model('botend', 'WowTalentVersion')._base_manager.using(using).select_for_update().get(pk=version.pk)
        kind, identity = row['kind'], row['object_id']
        model = registry.get_model('botend', 'WowTalentNodeMetadata')
        query = model._base_manager.using(using).filter(talent_version=version, name_kind=kind)
        if target_pk is not None:
            if isinstance(target_pk, bool) or not isinstance(target_pk, int) or target_pk < 1:
                raise ValidationError('名称记录编号无效')
            if not isinstance(target_state, dict) or any(
                    not isinstance(target_state.get(field), str) for field in EDIT_STATE_FIELDS):
                raise ValidationError('名称编辑状态无效，请刷新后重试')
            expected_count = target_state.get('duplicate_count')
            if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
                raise ValidationError('名称编辑状态无效，请刷新后重试')
            # Read once to determine the small identity scope, then lock that scope in PK order.
            # Exact text matching stays in Python so MySQL collations cannot merge labels.
            preliminary = query.filter(pk=target_pk).first()
            if preliminary is None:
                raise NameEditConflict('名称记录已变化，请刷新后重试')
            if preliminary.localization_only:
                candidates = query.filter(pk=target_pk)
            elif preliminary.talent_id:
                candidates = query.filter(localization_only=False, talent_id=preliminary.talent_id)
            elif preliminary.node_id:
                candidates = query.filter(localization_only=False, talent_id__isnull=True, node_id=preliminary.node_id)
            else:
                candidates = query.filter(localization_only=False, talent_id__isnull=True,
                                          node_id__isnull=True, spell_id=preliminary.spell_id)
            candidates = list(candidates.select_for_update().order_by('pk'))
            target = next((obj for obj in candidates if obj.pk == target_pk), None)
            target_identity = target and (
                target.reference_id if target.localization_only else target.talent_id or target.node_id or target.spell_id)
            current_state = target and dict(name_en=target.name, name_zh=target.name_zh,
                                            icon=target.icon, evidence=target.localization_evidence)
            expected_state = {field: target_state[field] for field in EDIT_STATE_FIELDS}
            if target is None or target_identity != identity or current_state != expected_state:
                raise NameEditConflict('名称记录已变化，请刷新后重试')
            if target.localization_evidence and not row['evidence']:
                raise ValidationError('请保留或更新名称核对依据')
            targets = [obj for obj in candidates if dict(
                name_en=obj.name, name_zh=obj.name_zh, icon=obj.icon,
                evidence=obj.localization_evidence) == expected_state]
            if len(targets) != expected_count:
                raise NameEditConflict('名称记录已变化，请刷新后重试')
            for obj in targets:
                changed = []
                for field, value in [('name', row['name_en']), ('name_zh', row['name_zh']), ('icon', row['icon']), ('localization_evidence', row['evidence'])]:
                    if getattr(obj, field) != value:
                        setattr(obj, field, value); changed.append(field)
                if changed:
                    obj.save(using=using, update_fields=changed)
            return _record(targets[0], kind, row['game_version'], identity), False
        native = []
        if kind == 'talent':
            native = list(query.filter(localization_only=False).filter(Q(talent_id=identity) | Q(node_id=identity)))
            if not native:
                native = [obj for obj in query.filter(localization_only=False) if identity in obj.reference_aliases]
            if not native and row['name_en']:
                native = list(query.filter(localization_only=False, name__iexact=row['name_en']))
            if (len({obj.name for obj in native if obj.name}) > 1
                    or len({obj.icon for obj in native if obj.icon}) > 1
                    or len({obj.name_zh for obj in native if obj.name_zh}) > 1):
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
                current = getattr(obj, field)
                if (overwrite or not current) and current != value and (value or not preserve_blank):
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
