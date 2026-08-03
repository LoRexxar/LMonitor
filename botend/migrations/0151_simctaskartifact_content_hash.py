from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0150_normalize_mid_profile_equipment'),
    ]

    operations = [
        migrations.AddField(
            model_name='simctaskartifact',
            name='content_hash',
            field=models.CharField(
                blank=True,
                default='',
                help_text='完成时验证的产物SHA-256',
                max_length=64,
            ),
        ),
    ]
