from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('botend', '0280_wcl_consumable_observation')]

    operations = [
        migrations.AddIndex(
            model_name='specdungeonranking',
            index=models.Index(
                fields=['season_id', 'region', 'character_name', 'realm', '-fight_started_at'],
                name='idx_dungeon_character_fights',
            ),
        ),
    ]
