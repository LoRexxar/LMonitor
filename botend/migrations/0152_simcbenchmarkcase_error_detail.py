from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0151_simctaskartifact_content_hash'),
    ]

    operations = [
        migrations.AddField(
            model_name='simcbenchmarkcase',
            name='error_detail',
            field=models.TextField(blank=True, default=''),
        ),
    ]
