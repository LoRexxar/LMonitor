"""后台组合资料提示：按需生成简要资料，完整展示由前台承担。"""

from urllib.parse import urlencode

from django.urls import reverse

from botend.models import WowTalentVersion
from botend.services.simc_player_config import build_player_config_detail, canonical_simc_profile_key
from botend.services.simc_reference_display import equipment_display, reference_trees
from botend.services.simc_apl.readable import display_symbols, readable_apl


RESOURCE_LABELS = {'profile': '装备', 'talent': '天赋', 'apl': 'APL'}


def resource_is_shared(resource, kind, config):
    if resource is None:
        return False
    if canonical_simc_profile_key(resource.spec, getattr(resource, 'class_name', '')) != config.spec_key:
        return False
    if kind == 'profile':
        return resource.user_id is None and resource.use_ptr == config.use_ptr
    return resource.owner_user_id is None and resource.is_system


def resource_links(config, resources):
    """携带完整组合身份；缺失或变为个人资源的项目不生成提示入口。"""
    query = urlencode({'config': config.pk,
                       **{kind: resource.pk if resource else 0 for kind, resource in resources.items()}})
    return {kind: reverse('simc_reference_resource_preview', kwargs={
                'spec_key': config.spec_key, 'kind': kind, 'resource_id': resource.pk,
            }) + '?' + query
            for kind, resource in resources.items() if resource_is_shared(resource, kind, config)}


def resource_display(kind, resource, spec, config):
    """仅解析正在查看的资料，避免每次切换都生成三类详情。"""
    if kind == 'profile':
        fields = (('gear_strength', '力量'), ('gear_crit', '暴击'), ('gear_haste', '急速'),
                  ('gear_mastery', '精通'), ('gear_versatility', '全能'))
        detail = build_player_config_detail(
            mode=resource.player_config_mode, spec=resource.spec,
            player_equipment=resource.player_equipment, talent='',
            battlenet_region=resource.battlenet_region, battlenet_realm=resource.battlenet_realm,
            battlenet_character=resource.battlenet_character,
            **{field: getattr(resource, field) for field, _ in fields},
        )
        return {'equipment_rows': equipment_display(detail),
                'attribute_overrides': [{'label': label, 'value': getattr(resource, field)}
                                        for field, label in fields if getattr(resource, field) is not None]}
    version = WowTalentVersion.objects.filter(
        is_active=True, branch='ptr' if config.use_ptr else 'retail',
    ).order_by('-is_default_simulator', '-updated_at', '-id').first()
    if kind == 'talent':
        return {'talent_display': reference_trees(resource.talent, spec, version)}
    class_key, spec_key = config.spec_key.split('_', 1)
    return {'apl_display': readable_apl(resource.content, display_symbols(
        class_key, spec_key, version, branch='ptr' if config.use_ptr else 'retail',
    )) if resource.content else {'groups': [], 'unsupported': [], 'count': 0}}


def resource_preview(kind, resource, spec, config, *, is_public):
    """仅返回有界的纯文本摘要，不在后台复制前台展示面板。"""
    display = resource_display(kind, resource, spec, config)
    lines = [f"{spec['label']} · {'PTR' if config.use_ptr else '正式服'}",
             '当前资源版本；历史模拟输入以报告为准。']
    if not is_public:
        lines.append('历史组合，已不在当前公开范围。')
    if kind == 'profile':
        rows = display['equipment_rows']
        lines.extend(f"{row['slot_label']}：{row.get('display_name') or row.get('name') or '未收录名称'}"
                     + (f" · {row['item_level']} 装等" if row.get('item_level') else '') for row in rows)
        if not rows:
            lines.append('未保存可解析的装备明细。')
        overrides = display['attribute_overrides']
        if overrides:
            lines.append('属性覆盖：' + ' · '.join(f"{row['label']} {row['value']}" for row in overrides))
    elif kind == 'talent':
        tree_display = display['talent_display']
        if not tree_display['trees']:
            lines.append(tree_display['message'])
        return {'name': resource.name, 'description': '\n'.join(lines),
                'talent_render_model': tree_display.get('render_model')}
    else:
        groups = display['apl_display']['groups']
        for group in groups[:6]:
            count = len(group['rows'])
            lines.append(f"{group['label']} · {count} 项" + ('（展示前 3 项）' if count > 3 else ''))
            for row in group['rows'][:3]:
                name = (row.get('reference') or {}).get('name') or row['label']
                guide = '暂未解析，请查看原文。' if row.get('unsupported') else row.get('guide_text', '')
                lines.append(f"{row['priority']}. {name}：{guide}")
        if len(groups) > 6:
            lines.append(f'另有 {len(groups) - 6} 个分组。')
        if not groups:
            lines.append('当前 APL 未提供可解析的优先级摘要。')
    lines.append('完整展示请打开对应的前台配置。')
    return {'name': resource.name, 'description': '\n'.join(lines)}
