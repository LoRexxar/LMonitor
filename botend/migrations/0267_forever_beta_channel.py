"""Reclassify stored Forever Beta channels without changing source evidence."""
from django.db import migrations


FORUM = 'https://us.forums.blizzard.com/en/wow'


def update_subtree(apps, schema_editor, version):
    Category = apps.get_model('botend', 'BlizzardForumCategory')
    rows = Category.objects.using(schema_editor.connection.alias).filter(forum_url=FORUM)
    pending = set(rows.filter(external_id=349).values_list('pk', flat=True))
    seen = set()
    while pending:
        seen.update(pending)
        pending = set(rows.filter(parent_id__in=pending).values_list('pk', flat=True)) - seen
    if seen:
        rows.filter(pk__in=seen).update(game_version=version)


def forwards(apps, schema_editor):
    update_subtree(apps, schema_editor, 'forever')


def backwards(apps, schema_editor):
    update_subtree(apps, schema_editor, 'test')


class Migration(migrations.Migration):
    dependencies = [('botend', '0266_blizzard_forum_metadata')]
    operations = [migrations.RunPython(forwards, backwards)]
