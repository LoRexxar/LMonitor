# Generated by Django 3.0.7 on 2023-06-07 08:17

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0009_auto_20230606_1759'),
    ]

    operations = [
        migrations.RenameField(
            model_name='vulndata',
            old_name='reference',
            new_name='link',
        ),
    ]
