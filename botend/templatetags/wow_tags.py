import os
from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def wow_icon(icon_name, size='small'):
    """返回本地或 CDN 图标路径。

    优先使用本地 static/wow_icons/{size}/{icon_name}.jpg，
    如果本地不存在则回退到 Wowhead CDN。
    """
    if not icon_name:
        return ''
    icon_name = str(icon_name).strip()
    if not icon_name:
        return ''
    if '.' in icon_name:
        icon_name = icon_name.rsplit('/', 1)[-1]
        icon_name = icon_name.rsplit('.', 1)[0]

    local_rel = os.path.join('wow_icons', size, f'{icon_name}.jpg')
    full_path = os.path.join(settings.BASE_DIR, 'static', local_rel)

    if os.path.exists(full_path):
        return f'/static/{local_rel}'

    # Fallback to Wowhead CDN
    return f'https://wow.zamimg.com/images/wow/icons/{size}/{icon_name}.jpg'
