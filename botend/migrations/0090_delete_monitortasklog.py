# Generated manually to remove redundant monitor execution log table

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0089_monitortasklog'),
    ]

    operations = [
        migrations.DeleteModel(
            name='MonitorTaskLog',
        ),
    ]
