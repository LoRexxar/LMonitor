from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0061_portalpeakspecrankrow'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowspellsnapshot',
            name='name_zh',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
