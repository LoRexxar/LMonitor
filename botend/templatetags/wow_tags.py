import os
from django import template
from django.conf import settings

register = template.Library()


def _normalize_icon_name(icon_name):
    icon_name = str(icon_name or '').strip()
    if not icon_name:
        return ''
    icon_name = icon_name.split('?', 1)[0].strip()
    icon_name = icon_name.rsplit('/', 1)[-1].strip()
    while '.' in icon_name:
        base, ext = icon_name.rsplit('.', 1)
        if ext.lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
            icon_name = base
            continue
        break
    return icon_name.strip()


@register.simple_tag
def wow_icon(icon_name, size='small'):
    """返回本地 wow 图标路径。

    优先使用本地 static/wow_icons/{size}/{icon_name}.jpg，
    如果本地不存在则回退到站内占位图，避免外链被浏览器拦截。
    """
    icon_name = _normalize_icon_name(icon_name)
    if not icon_name:
        icon_name = 'inv_misc_questionmark'

    candidate_sizes = []
    for candidate in (size, 'small'):
        candidate = str(candidate or '').strip() or 'small'
        if candidate not in candidate_sizes:
            candidate_sizes.append(candidate)

    for candidate_size in candidate_sizes:
        local_rel = os.path.join('wow_icons', candidate_size, f'{icon_name}.jpg')
        full_path = os.path.join(settings.BASE_DIR, 'static', local_rel)
        if os.path.exists(full_path):
            return f"/static/{local_rel.replace(os.sep, '/')}"

    placeholder_rel = os.path.join('wow_icons', 'small', 'inv_misc_questionmark.jpg')
    placeholder_path = os.path.join(settings.BASE_DIR, 'static', placeholder_rel)
    if os.path.exists(placeholder_path):
        return f"/static/{placeholder_rel.replace(os.sep, '/')}"

    return ''
