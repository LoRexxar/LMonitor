from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0087_add_db2_tree_component_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowarticle',
            name='content_blocks',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wowarticle',
            name='content_blocks_cn',
            field=models.TextField(blank=True, null=True),
        ),
    ]
