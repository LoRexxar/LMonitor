import importlib
from types import SimpleNamespace
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from botend.models import BlizzardForumCategory


class ForeverBetaChannelMigrationTests(TestCase):
    def test_only_official_beta_subtree_label_changes_and_is_reversible(self):
        forum = 'https://us.forums.blizzard.com/en/wow'
        observed = timezone.now()
        def category(external_id, parent=None, forum_url=forum):
            return BlizzardForumCategory.objects.create(
                forum_url=forum_url, external_id=external_id, name=str(external_id),
                slug=str(external_id), url=forum_url+'/c/'+str(external_id),
                parent=parent, game_version='test', last_observed_at=observed)
        development = category(23)
        beta = category(349, development)
        child = category(900002, beta)
        ptr = category(351, development)
        foreign = category(349, forum_url='https://example.org')
        before = list(BlizzardForumCategory.objects.order_by('pk').values())
        migration = importlib.import_module('botend.migrations.0267_forever_beta_channel')
        schema = SimpleNamespace(connection=connection)
        migration.forwards(apps, schema)
        after = list(BlizzardForumCategory.objects.order_by('pk').values())
        for old, new in zip(before, after):
            expected = dict(old)
            if old['id'] in {beta.pk, child.pk}:
                expected['game_version'] = 'forever'
            self.assertEqual(new, expected)
        migration.forwards(apps, schema)
        self.assertEqual(list(BlizzardForumCategory.objects.order_by('pk').values()), after)
        migration.backwards(apps, schema)
        self.assertEqual(list(BlizzardForumCategory.objects.order_by('pk').values()), before)
