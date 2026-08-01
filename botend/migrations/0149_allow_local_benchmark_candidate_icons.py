from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0148_clear_implicit_profile_attribute_overrides'),
    ]

    operations = [
        migrations.AlterField(
            model_name='simcbenchmarkcandidate',
            name='icon_url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
