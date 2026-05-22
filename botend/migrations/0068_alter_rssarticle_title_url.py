from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0067_remove_wowarticle_created_at_updated_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rssarticle',
            name='title',
            field=models.CharField(default=None, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='rssarticle',
            name='url',
            field=models.CharField(default=None, max_length=2000, null=True),
        ),
    ]

