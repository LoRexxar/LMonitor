from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0068_alter_rssarticle_title_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitortask',
            name='proxy_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='monitortask',
            name='proxy_url',
            field=models.CharField(default=None, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='monitortask',
            name='proxy_bypass',
            field=models.CharField(default=None, max_length=1000, null=True),
        ),
        migrations.AddField(
            model_name='monitortask',
            name='proxy_scope',
            field=models.IntegerField(default=2),
        ),
    ]

