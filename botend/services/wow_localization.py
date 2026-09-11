"""全站名称读写：复用天赋节点、法术和物品原表，不保存第二份覆盖表。"""
import hashlib
import re
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.db.models import Case, IntegerField, Q, Value, When

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


def current_reference_version(registry=apps, using='default'):
    """Return the current retail source; guide versions never select a data bucket."""
    model = registry.get_model('botend', 'WowTalentVersion')
    query = model._base_manager.using(using)
    retail = query.filter(branch='retail', is_active=True).order_by(
        '-is_default_player_tree', '-id',
    ).first()
    if retail:
        return retail
    return query.filter(is_active=True).order_by(
        '-is_default_player_tree', '-id',
    ).first()


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


def _spell_snapshot_branches(version):
    return ['wowxptr', 'wowt'] if version.branch == 'ptr' else ['wow']


def _inferred_reference(identity, *, registry, using):
    """无攻略引用时仅根据现有权威表推断；多类型命中必须拒绝猜测。"""
    metadata_model = registry.get_model('botend', 'WowTalentNodeMetadata')
    metadata = metadata_model._base_manager.using(using).select_related('talent_version')
    existing = list(metadata.filter(localization_only=True, reference_id=identity))
    talent_rows = list(metadata.filter(localization_only=False, node_id=identity))
    if not talent_rows:
        aliases = metadata.filter(localization_only=False)
        if connections[using].features.supports_json_field_contains:
            talent_rows = list(aliases.filter(reference_aliases__contains=[identity]))
        else:
            talent_rows = [obj for obj in aliases if identity in obj.reference_aliases]
    spell_rows = list(metadata.filter(localization_only=False).filter(
        Q(spell_id=identity) | Q(display_spell_id=identity)))
    raw_spell_snapshots = list(
        registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(
            spell_id=identity, locale='enUS'))
    kinds = {obj.name_kind for obj in existing}
    if talent_rows:
        kinds.add('talent')
    if spell_rows or raw_spell_snapshots:
        kinds.add('spell')
    if registry.get_model('botend', 'WowItemSnapshot')._base_manager.using(using).filter(
            item_id=identity).exists():
        kinds.add('item')
    if len(kinds) != 1:
        if not kinds:
            raise ValidationError('该编号未出现在攻略或权威快照中，无法自动识别类型')
        raise ValidationError('该编号匹配多个对象类型，无法安全自动识别')
    return kinds.pop()


def _generated_reference_metadata(kind, identity, version, guide_label, *, registry, using):
    name_en = icon = ''
    sources = []
    metadata_model = registry.get_model('botend', 'WowTalentNodeMetadata')
    native = metadata_model._base_manager.using(using).filter(localization_only=False)
    if version:
        native = native.filter(talent_version=version)
    if kind == 'talent':
        native = list(native.filter(node_id=identity))
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

    if kind == 'spell':
        preferred_branches = _spell_snapshot_branches(version)
        snapshots = list(registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(
            spell_id=identity,
            locale='enUS',
        ))
        source = min(snapshots, key=lambda row: (
            preferred_branches.index(row.branch) if row.branch in preferred_branches else len(preferred_branches),
            row.branch,
        ), default=None)
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
    else:
        kind = _inferred_reference(identity, registry=registry, using=using)
    version = current_reference_version(registry, using)
    if not version:
        raise ValidationError('没有可用于名称资料的当前权威游戏版本')
    guide_label = next((label for _, reference_kind, label in references
                        if reference_kind == kind and label), '')
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
        description=getattr(obj, 'description', '') or '',
        description_zh=getattr(obj, 'description_zh', '') or '',
        model=obj._meta.object_name, pk=obj.pk, supplemental=obj.localization_only,
        aliases=obj.reference_aliases, class_name=obj.class_name, spec_name=obj.spec_name,
        reference_id=obj.reference_id, talent_id=obj.talent_id, node_id=obj.node_id,
        spell_id=obj.spell_id, display_spell_id=obj.display_spell_id,
        tree_type=obj.tree_type)


def names_for(game_version, class_name='', spec_name='', registry=apps, using='default', *, reference_ids=None, source_text=None):
    """所有类型同表读取；名称资料与真实节点按标志区分。"""
    if reference_ids is None and source_text is not None:
        refs = re.findall(r'\[\[(spell|talent|item):(\d+)', source_text)
        if refs:
            reference_ids = {
                kind: {int(identity) for typ, identity in refs if typ == kind}
                for kind in ('spell', 'talent', 'item')
            }
    has_reference_filter = reference_ids is not None
    version = (current_reference_version(registry, using)
               if has_reference_filter else version_for(game_version, registry, using))
    if not version:
        return []
    game_version = version_label(version, game_version)
    query = registry.get_model('botend', 'WowTalentNodeMetadata')._base_manager.using(using)
    if has_reference_filter:
        # 显式身份跨 active branch 查询；攻略 game_version 不选择数据桶。
        # 同号原生事实由 retail 优先，PTR-only ID 仍可全局命中。
        query = query.filter(
            Q(localization_only=True) | Q(talent_version__is_active=True)
        ).annotate(
            _reference_branch_order=Case(
                When(talent_version__branch='retail', then=Value(0)),
                When(talent_version__branch='ptr', then=Value(1)),
                default=Value(2), output_field=IntegerField(),
            ),
            _reference_version_order=Case(
                When(talent_version__key__in=['retail', 'ptr'], then=Value(0)),
                default=Value(1), output_field=IntegerField(),
            ),
        )
    else:
        query = query.filter(talent_version=version)
    query = query.exclude(name_zh='')
    reference_ids = reference_ids or {}
    talent_reference_ids = {int(value) for value in reference_ids.get('talent', set())}
    spell_reference_ids = {int(value) for value in reference_ids.get('spell', set())}
    talent_alias_pks = []
    if talent_reference_ids:
        direct_rows = query.filter(name_kind='talent').filter(
            Q(node_id__in=talent_reference_ids) | Q(reference_id__in=talent_reference_ids)
        ).values_list('node_id', 'reference_id')
        direct_ids = {int(value) for row in direct_rows for value in row if value}
        unmatched_ids = talent_reference_ids - direct_ids
        if unmatched_ids:
            if connections[using].features.supports_json_field_contains:
                alias_selection = Q()
                for value in unmatched_ids:
                    alias_selection |= Q(reference_aliases__contains=[value])
                talent_alias_pks = query.filter(name_kind='talent').filter(alias_selection).values('pk')
            else:
                # SQLite 测试后端不支持 JSON contains；仅测试/开发环境做 Python 匹配。
                talent_alias_pks = [
                    pk for pk, aliases in query.filter(name_kind='talent').values_list('pk', 'reference_aliases')
                    if unmatched_ids.intersection(int(value) for value in (aliases or []))
                ]
    if class_name:
        context = Q(localization_only=True) | Q(class_name__iexact=class_name, spec_name__iexact=spec_name)
        if spell_reference_ids:
            # 显式技能 ID 可以命中其他职业/专精天赋节点所承载的 Spell ID。
            context |= Q(name_kind='talent') & (
                Q(spell_id__in=spell_reference_ids) | Q(display_spell_id__in=spell_reference_ids)
            )
        if talent_reference_ids:
            # 攻略 talent:ID 是来源 TraitNodeEntry.ID（站内 node_id），不受攻略
            # 职业/专精上下文限制。TraitNode.ID（talent_id）是另一 ID 空间。
            context |= Q(name_kind='talent') & (
                Q(node_id__in=talent_reference_ids) | Q(reference_id__in=talent_reference_ids) |
                Q(pk__in=talent_alias_pks)
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
        # 显式引用只加载与 (kind, ID) 对应的行；不把当前专精的全部天赋扩进候选集。
        selection = Q(pk__in=[])
        if talent_reference_ids:
            selection |= Q(name_kind='talent') & (
                Q(node_id__in=talent_reference_ids) | Q(reference_id__in=talent_reference_ids) |
                Q(pk__in=talent_alias_pks)
            )
        if spell_reference_ids:
            selection |= Q(name_kind='spell', reference_id__in=spell_reference_ids)
            selection |= Q(name_kind='talent') & (
                Q(spell_id__in=spell_reference_ids) | Q(display_spell_id__in=spell_reference_ids)
            )
        item_reference_ids = {int(value) for value in reference_ids.get('item', set())}
        if item_reference_ids:
            selection |= Q(name_kind='item', reference_id__in=item_reference_ids)
        query = query.filter(selection)
    records = []
    order_fields = ['localization_only']
    if has_reference_filter:
        order_fields.extend(['_reference_branch_order', '_reference_version_order'])
    order_fields.append('id')
    for obj in query.only('id','name','name_zh','icon','localization_evidence','localization_only','name_kind',
            'reference_id','reference_aliases','talent_id','node_id','spell_id','display_spell_id','tree_type','class_name','spec_name',
            'description','description_zh').order_by(*order_fields):
        identity = obj.reference_id if obj.localization_only else obj.node_id or obj.talent_id or obj.spell_id
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
        preferred_branches = _spell_snapshot_branches(version)
        snapshots = registry.get_model('botend', 'WowSpellSnapshot')._base_manager.using(using).filter(
            spell_id=row['object_id']).exclude(name_zh='')
        candidates = [snapshot for snapshot in snapshots
                      if re.search(r'[\u3400-\u9fff]', snapshot.name_zh)]
        source = max(candidates, key=lambda snapshot: (
            snapshot.branch in preferred_branches,
            snapshot.locale == 'zhCN',
            snapshot.snapshot_build,
        ), default=None)
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
                candidates = model._base_manager.using(using).filter(
                    localization_only=True,
                    name_kind=kind,
                    reference_id=preliminary.reference_id,
                )
            elif preliminary.talent_id:
                candidates = query.filter(localization_only=False, talent_id=preliminary.talent_id)
            elif preliminary.node_id:
                candidates = query.filter(localization_only=False, talent_id__isnull=True,
                                          node_id=preliminary.node_id)
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
            native = list(query.filter(localization_only=False, node_id=identity))
            if not native:
                native = [obj for obj in query.filter(localization_only=False) if identity in obj.reference_aliases]
            if not native and row['name_en']:
                native = list(query.filter(localization_only=False, name__iexact=row['name_en']))
            if (len({obj.name for obj in native if obj.name}) > 1
                    or len({obj.icon for obj in native if obj.icon}) > 1
                    or len({obj.name_zh for obj in native if obj.name_zh}) > 1):
                native = []
        supplemental = model._base_manager.using(using).filter(
            localization_only=True, name_kind=kind, reference_id=identity,
        )
        targets = native or list(supplemental)
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
