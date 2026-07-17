import hashlib

from django.db import migrations, models
from django.db.models import Value
from django.db.models.functions import Replace


OLD_PREFIX = 'https://nga.178.com/'
NEW_PREFIX = 'https://bbs.nga.cn/'


def _url_hash(url):
    return hashlib.sha256(str(url).encode('utf-8')).hexdigest()


def _replace_text_fields(apps):
    """Replace the retired NGA origin in every concrete text column."""
    skipped = {
        ('botend', 'wowarticle', 'url'),
        ('botend', 'wowarticle', 'url_hash'),
        ('botend', 'portalevent', 'url'),
        ('botend', 'portalevent', 'url_hash'),
        ('botend', 'portaltoollink', 'url'),
        ('botend', 'portaltoollink', 'url_hash'),
        ('botend', 'videomonitortarget', 'target_url'),
        ('botend', 'videomonitortarget', 'target_url_hash'),
        ('botend', 'portalvideo', 'url'),
        ('botend', 'portalvideo', 'url_hash'),
    }
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            key = (model._meta.app_label, model._meta.model_name, field.name)
            if key in skipped or not isinstance(field, (models.CharField, models.TextField)):
                continue
            model.objects.filter(**{f'{field.name}__contains': OLD_PREFIX}).update(
                **{field.name: Replace(field.name, Value(OLD_PREFIX), Value(NEW_PREFIX))}
            )


def _merge_article(old_article, current_article):
    """Keep the current-domain row while preserving useful data from its legacy twin."""
    text_fields = (
        'title', 'title_cn', 'author', 'description', 'content', 'content_cn',
        'content_blocks', 'content_blocks_cn', 'source', 'category',
    )
    for field in text_fields:
        old_value = getattr(old_article, field, None)
        current_value = getattr(current_article, field, None)
        if isinstance(old_value, str):
            old_value = old_value.replace(OLD_PREFIX, NEW_PREFIX)
        if field == 'category' and old_value == 'nga':
            setattr(current_article, field, old_value)
        elif field == 'author' and old_value == 'nga前瞻区':
            setattr(current_article, field, old_value)
        elif old_value and not current_value:
            setattr(current_article, field, old_value)
    current_article.reply_count = max(
        int(current_article.reply_count or 0), int(old_article.reply_count or 0)
    )
    current_article.is_active = bool(current_article.is_active or old_article.is_active)
    if old_article.publish_time and (
        not current_article.publish_time or old_article.publish_time > current_article.publish_time
    ):
        current_article.publish_time = old_article.publish_time
    current_article.save()
    old_article.delete()


def _replace_article_urls(apps):
    WowArticle = apps.get_model('botend', 'WowArticle')
    legacy_rows = list(WowArticle.objects.filter(url__contains=OLD_PREFIX).order_by('id'))
    for old_article in legacy_rows:
        new_url = old_article.url.replace(OLD_PREFIX, NEW_PREFIX)
        current = WowArticle.objects.filter(url=new_url).exclude(pk=old_article.pk).first()
        if current:
            _merge_article(old_article, current)
            continue
        old_article.url = new_url
        old_article.url_hash = _url_hash(new_url)
        old_article.save(update_fields=['url', 'url_hash'])


def _replace_tool_urls(apps):
    PortalToolLink = apps.get_model('botend', 'PortalToolLink')
    for tool in PortalToolLink.objects.filter(url__contains=OLD_PREFIX).order_by('id'):
        new_url = tool.url.replace(OLD_PREFIX, NEW_PREFIX)
        current = PortalToolLink.objects.filter(url=new_url).exclude(pk=tool.pk).first()
        if current:
            for field in ('name', 'desc', 'source', 'icon_path'):
                if not getattr(current, field, None) and getattr(tool, field, None):
                    setattr(current, field, getattr(tool, field))
            current.is_active = bool(current.is_active or tool.is_active)
            current.is_topbar = bool(current.is_topbar or tool.is_topbar)
            if tool.sort_order and (not current.sort_order or tool.sort_order < current.sort_order):
                current.sort_order = tool.sort_order
            if tool.topbar_order and (
                not current.topbar_order or tool.topbar_order < current.topbar_order
            ):
                current.topbar_order = tool.topbar_order
            current.save()
            tool.delete()
            continue
        tool.url = new_url
        tool.url_hash = _url_hash(new_url)
        tool.save(update_fields=['url', 'url_hash'])


def _replace_hashed_urls(apps, model_name, url_field, hash_field, collision_fields=()):
    Model = apps.get_model('botend', model_name)
    rows = list(Model.objects.filter(**{f'{url_field}__contains': OLD_PREFIX}).order_by('id'))
    for old_row in rows:
        new_url = getattr(old_row, url_field).replace(OLD_PREFIX, NEW_PREFIX)
        new_hash = _url_hash(new_url)
        filters = {hash_field: new_hash}
        for field in collision_fields:
            filters[field] = getattr(old_row, field)
        current = Model.objects.filter(**filters).exclude(pk=old_row.pk).first()
        if current:
            if hasattr(current, 'is_active'):
                current.is_active = bool(current.is_active or old_row.is_active)
                current.save(update_fields=['is_active'])
            old_row.delete()
            continue
        setattr(old_row, url_field, new_url)
        setattr(old_row, hash_field, new_hash)
        old_row.save(update_fields=[url_field, hash_field])


def replace_nga_domain(apps, schema_editor):
    _replace_article_urls(apps)
    _replace_tool_urls(apps)
    _replace_hashed_urls(apps, 'PortalEvent', 'url', 'url_hash')
    _replace_hashed_urls(
        apps, 'VideoMonitorTarget', 'target_url', 'target_url_hash', ('platform',)
    )
    _replace_hashed_urls(apps, 'PortalVideo', 'url', 'url_hash')
    _replace_text_fields(apps)


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0111_simulationrun_task_sequence_unique'),
    ]

    operations = [
        migrations.RunPython(replace_nga_domain, migrations.RunPython.noop),
    ]
