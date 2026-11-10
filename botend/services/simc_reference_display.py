"""复用人物详情的天赋模型，为专精资料页准备只读展示。"""

import logging

from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.wow.talents.build_code import TalentBuildCodeDecoder
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.service import TalentBuildCodeService
from botend.wow.talents.view_model import build_talent_view_model

logger = logging.getLogger(__name__)


def reference_trees(code, spec, version):
    if not code or not version:
        return {'trees': [], 'message': '当前没有可展示的天赋树资料，仍可复制导入代码。'}
    identity = (spec['class_name'], spec['spec_name'])
    if TalentBuildCodeDecoder.resolve_spec_identity(code) != identity:
        return {'trees': [], 'message': '天赋代码无法匹配当前专精，暂不绘制天赋树。'}
    try:
        provider = TalentMetadataProvider(talent_version=version)
        payload = TalentBuildCodeService.build_full_payload(
            class_name=identity[0], spec_name=identity[1], talent_build_code=code,
            talent_version=version,
        )
        model = build_talent_view_model(
            payload, class_name=identity[0], spec_name=identity[1], metadata_provider=provider,
        )['render_model']
    except ValueError:
        logger.warning('专精资料天赋代码解析失败：%s', spec['key'], exc_info=True)
        return {'trees': [], 'message': '当前天赋元数据无法解析此方案，仍可复制导入代码。'}
    trees = []
    for tree in model.get('trees', []):
        panel = tree.get('panel')
        if not panel or not tree.get('nodes'):
            continue
        tree['points_spent'] = sum(node.get('points', 0) or 0 for node in tree['nodes'])
        trees.append(tree)
    return {'trees': trees, 'render_model': {**model, 'trees': trees},
            'message': '' if trees else '当前版本尚无此专精的天赋树资料。'}


def equipment_display(detail):
    """在已有的装备语义结果上补充提示，保留未知装备的真实 ID。"""
    rows = []
    for item in detail.get('equipment') or []:
        row = dict(item)
        row['tooltip_text'] = item.get('tooltip') or item.get('display_description') or item.get('description_zh') or item.get('description') or '当前资料尚未提供属性说明。'
        row['display_icon'] = item.get('icon_url') or (wow_icon_oss_url(item['icon']) if item.get('icon') else '')
        for enhancement in [item.get('enchant'), *(item.get('gems') or [])]:
            if not enhancement:
                continue
            enhancement['tooltip_text'] = enhancement.get('tooltip') or enhancement.get('display_description') or enhancement.get('description_zh') or enhancement.get('description') or '当前资料尚未提供说明。'
            enhancement['display_icon'] = enhancement.get('icon_url') or (wow_icon_oss_url(enhancement['icon']) if enhancement.get('icon') else '')
        rows.append(row)
    return rows
