import re

from django.db import migrations


LIVE_TITLE_PREFIX_RE = re.compile(r"^\s*\[\s*live\s*\]\s*", re.IGNORECASE)
TRANSLATED_LIVE_PREFIX_RE = re.compile(r"^\s*\[(?:直播|实时|正式服)\]\s*")


def repair_wowhead_live_titles(apps, schema_editor):
    WowArticle = apps.get_model("botend", "WowArticle")
    articles = (
        WowArticle.objects.filter(source="wowhead", title__iregex=r"^\s*\[\s*live\s*\]")
        .exclude(title_cn__isnull=True)
        .exclude(title_cn="")
        .only("id", "title", "title_cn")
    )
    updates = []
    for article in articles.iterator(chunk_size=500):
        if not LIVE_TITLE_PREFIX_RE.match(article.title or ""):
            continue
        translated_title = TRANSLATED_LIVE_PREFIX_RE.sub("", (article.title_cn or "").strip(), count=1)
        normalized_title = "[正式服]{}".format(translated_title)
        if normalized_title != article.title_cn:
            article.title_cn = normalized_title
            updates.append(article)
    if updates:
        WowArticle.objects.bulk_update(updates, ["title_cn"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0159_simctaskfavorite"),
    ]

    operations = [
        migrations.RunPython(repair_wowhead_live_titles, migrations.RunPython.noop),
    ]