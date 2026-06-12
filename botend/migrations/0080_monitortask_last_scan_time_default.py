from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0079_add_spec_detail_tables'),
    ]

    operations = [
        migrations.AlterField(
            model_name='monitortask',
            name='last_scan_time',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
    ]
