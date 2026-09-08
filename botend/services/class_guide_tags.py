"""攻略的可编辑标签；来源版本仅用于同步身份与术语解析。"""
from urllib.parse import urlparse

from botend.guide_models import ClassGuideTag

TYPE_LABELS = {'raid': '团本', 'mythic-plus': '大秘境', 'leveling': '练级', 'pvp': '玩家对战', 'general': '通用'}


def normalize_tags(values):
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError('标签必须是列表，每篇最多 30 个')
    names = []
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
            raise ValueError('每个标签必须为 1 至 60 个字符')
        name = value.strip()
        if name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return names


def set_guide_tags(guide, names):
    records = []
    for name in normalize_tags(names):
        tag = ClassGuideTag.objects.filter(name__iexact=name).first()
        if tag is None:
            tag, _ = ClassGuideTag.objects.get_or_create(name=name)
        records.append(tag)
    guide.tags.set(records)


def source_labels(version, kind):
    return [version, TYPE_LABELS.get(kind, kind)]


def guide_disclaimers(guide):
    return [tag.disclaimer.strip() for tag in guide.tags.all() if tag.disclaimer.strip()]


def ensure_source_tag(guide):
    """导入时补充来源标签，保留已有的人工标签。"""
    if urlparse(guide.source_url).hostname not in ('maxroll.gg', 'www.maxroll.gg'):
        return
    tag = ClassGuideTag.objects.filter(name__iexact='maxroll').first()
    if tag is None:
        tag, _ = ClassGuideTag.objects.get_or_create(name='maxroll')
    guide.tags.add(tag)
