"""面向内测用户的现行 SimC 资料投影；只读取全局资源和本地展示元数据。"""

from django.http import Http404
from botend.services.simc_talent_titles import apply_talent_display_names

from botend.constants.wow import CLASS_CN, CLASS_SPEC_MAP, SPEC_CN, SPEC_ICON
from botend.models import SimcApl, SimcProfile, SimcTalentString, WowTalentVersion, SimcReferencePublication
from botend.services.simc_player_config import (
    build_player_config_detail, canonical_simc_profile_key,
)
from botend.services.simc_reference_display import equipment_display, reference_trees
from botend.services.simc_apl.readable import display_symbols, readable_apl
from botend.services.simc_reference_heroes import (
    hero_selection, filter_talent_rows, filter_tree_panels, tree_hero_ids,
)


def reference_catalog(*, public_only=True):
    """一次读取轻量目录；不将所有专精的装备正文或 APL 装入页面。"""
    groups, by_key = [], {}
    for class_name, specs in CLASS_SPEC_MAP.items():
        group = {'name': CLASS_CN[class_name], 'specs': []}
        for spec_name in specs:
            key = canonical_simc_profile_key(
                'beast_mastery' if spec_name == 'BeastMastery' else spec_name.lower(),
                class_name,
            )
            row = {
                'key': key, 'class_name': class_name, 'spec_name': spec_name,
                'label': f'{CLASS_CN[class_name]} · {SPEC_CN[spec_name]}',
                'icon': SPEC_ICON.get((class_name, spec_name), ''),
                'profiles': [], 'talents': [], 'apls': [],
            }
            group['specs'].append(row)
            by_key[key] = row
        groups.append(group)

    querysets = {
        'profiles': SimcProfile.objects.filter(is_active=True, user_id__isnull=True).values(
            'id', 'name', 'spec', 'class_name', 'system_key', 'source', 'version', 'profile_set', 'use_ptr',
        ),
        'talents': SimcTalentString.objects.filter(
            is_active=True, is_selectable=True, is_system=True, owner_user_id__isnull=True,
        ).values('id', 'name', 'spec', 'talent', 'system_key', 'default_apl_id', 'hero_talent_names'),
        'apls': SimcApl.objects.filter(
            is_active=True, is_selectable=True, is_system=True, owner_user_id__isnull=True,
        ).values('id', 'name', 'spec', 'class_name', 'source'),
    }
    allowed = {}
    if public_only:
        published = SimcReferencePublication.objects.filter(is_published=True)
        for kind in querysets:
            allowed[kind] = {}
            for spec_key, resource_id in published.values_list('spec_key', f'{kind}__pk'):
                allowed[kind].setdefault(spec_key, set()).add(resource_id)
            querysets[kind] = querysets[kind].filter(pk__in=published.values_list(f'{kind}__pk', flat=True))
    for kind, queryset in querysets.items():
        resources = list(queryset.order_by('name', 'id'))
        if kind == 'talents':
            apply_talent_display_names(resources)
        for resource in resources:
            key = canonical_simc_profile_key(resource['spec'], resource.get('class_name', ''))
            if key in by_key and (not public_only or resource['id'] in allowed[kind].get(key, set())):
                by_key[key][kind].append(resource)
    for row in by_key.values():
        row['has_data'] = any(row[kind] for kind in querysets)
    return groups, by_key


def _select(rows, raw_id, preferred_id=None):
    if raw_id:
        if not raw_id.isascii() or not raw_id.isdigit():
            raise Http404('资料不存在')
        selected = next((row for row in rows if str(row['id']) == raw_id), None)
        if selected is None:
            raise Http404('资料不存在')
        return selected
    return next((row for row in rows if row['id'] == preferred_id), rows[0] if rows else None)


def reference_context(params):
    from botend.services.simc_reference_simulations import public_results
    groups, specs = reference_catalog()
    key = params.get('spec') or next(
        (key for key, row in specs.items() if row['has_data']), next(iter(specs)),
    )
    if key not in specs:
        raise Http404('专精不存在')
    spec = dict(specs[key])
    heroes = hero_selection(params, spec)
    stable_key = f'simc_upstream:{key}'
    profile_default = next((r['id'] for r in spec['profiles'] if r['system_key'] == stable_key), None)
    talent_default = next((r['id'] for r in spec['talents'] if r['system_key'] == stable_key), None)
    profile_row = _select(spec['profiles'], params.get('profile', ''), profile_default)
    talent_row = _select(spec['talents'], params.get('talent', ''), talent_default)
    profile = SimcProfile.objects.filter(
        pk=profile_row['id'], is_active=True, user_id__isnull=True,
        simcreferencepublication__spec_key=key, simcreferencepublication__is_published=True,
    ).first() if profile_row else None
    talent_version = WowTalentVersion.objects.filter(
        is_active=True, branch='ptr' if profile and profile.use_ptr else 'retail',
    ).order_by('-is_default_simulator', '-updated_at', '-id').first()
    spec['talents'], cached_displays = filter_talent_rows(spec['talents'], heroes['ids'], spec, talent_version)
    if heroes['ids']:
        preserved_id = talent_row['id'] if talent_row and talent_row in spec['talents'] else talent_default
        talent_row = _select(spec['talents'], '', preserved_id)
    # 天赋显式绑定的默认 APL 不可用时，不冒充继承了其他 APL。
    linked_apl_id = talent_row['default_apl_id'] if talent_row else None
    upstream_apls = [r for r in spec['apls'] if r['source'] == SimcApl.SOURCE_SIMC_UPSTREAM]
    default_apl_id = linked_apl_id or (upstream_apls[0]['id'] if len(upstream_apls) == 1 else None)
    if default_apl_id is None and len(spec['apls']) == 1:
        default_apl_id = spec['apls'][0]['id']
    apl_missing = bool(linked_apl_id and not any(r['id'] == linked_apl_id for r in spec['apls']))
    if params.get('apl') or default_apl_id and not apl_missing:
        apl_row = _select(spec['apls'], params.get('apl', ''), default_apl_id)
    else:
        apl_row = None

    # 再次约束可见性，避免目录读取后资源被停用或转为个人资源。
    talent = SimcTalentString.objects.filter(
        pk=talent_row['id'], is_active=True, is_selectable=True,
        is_system=True, owner_user_id__isnull=True,
        simcreferencepublication__spec_key=key, simcreferencepublication__is_published=True,
    ).first() if talent_row else None
    apl = SimcApl.objects.filter(
        pk=apl_row['id'], is_active=True, is_selectable=True,
        is_system=True, owner_user_id__isnull=True,
        simcreferencepublication__spec_key=key, simcreferencepublication__is_published=True,
    ).first() if apl_row else None
    detail = {}
    equipment_code = ''
    if profile:
        from simc_equipment_control import SLOTS
        allowed_keys = set(SLOTS) | {key.lower() for key in CLASS_CN} | {
            'spec', 'race', 'level', 'role', 'position', 'professions', 'food', 'flask', 'potion',
            'augmentation', 'temporary_enchant', 'main_hand', 'off_hand',
        }
        equipment_code = '\n'.join(line for line in profile.player_equipment.splitlines()
                                   if line.partition('=')[0].strip().lower() in allowed_keys)
        detail = build_player_config_detail(
            mode=profile.player_config_mode, spec=profile.spec,
            player_equipment=profile.player_equipment, talent=talent.talent if talent else '',
            battlenet_region=profile.battlenet_region, battlenet_realm=profile.battlenet_realm,
            battlenet_character=profile.battlenet_character,
            **{field: getattr(profile, field) for field in (
                'gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery', 'gear_versatility',
            )},
        )
    # 装备中的内嵌天赋不具有发布授权，不能作为未勾选天赋的回退来源。
    if detail and not talent:
        detail['talents'] = {'build_code': ''}
    talent_code = talent.talent if talent else ''
    tree_display = cached_displays.get(talent.id) if talent else None
    if tree_display is None:
        tree_display = reference_trees(talent_code, spec, talent_version)
    if heroes['ids'] and not talent and not tree_hero_ids(tree_display) & heroes['ids']:
        talent_code = ''
        tree_display = {'trees': [], 'message': '暂无符合所选英雄天赋的天赋方案。'}
    tree_display = filter_tree_panels(tree_display, heroes['ids'])
    class_key, spec_key = key.split('_', 1)
    apl_display = readable_apl(apl.content, display_symbols(
        class_key, spec_key, talent_version,
        branch='ptr' if profile and profile.use_ptr else 'retail',
    ), hero_id=heroes['apl_id']) if apl and apl.content else {'groups': [], 'unsupported': [], 'count': 0}
    overrides = []
    if profile:
        for field, label in (
            ('gear_strength', '力量'), ('gear_crit', '暴击'), ('gear_haste', '急速'),
            ('gear_mastery', '精通'), ('gear_versatility', '全能'),
        ):
            if getattr(profile, field) is not None:
                overrides.append({'label': label, 'value': getattr(profile, field)})
    return {
        'spec_groups': groups, 'selected_spec': spec,
        'heroes': heroes,
        'profile': profile, 'talent': talent, 'apl': apl, 'player_detail': detail,
        'equipment_code': equipment_code,
        'talent_code': talent_code, 'talent_version': talent_version.key if talent_version else '',
        'talent_display': tree_display, 'equipment_rows': equipment_display(detail),
        'apl_display': apl_display,
        'apl_missing': apl_missing, 'apl_is_default': bool(apl and apl.id == default_apl_id),
        'attribute_overrides': overrides,
        'simulation_results': public_results(key, profile, talent, apl),
    }
