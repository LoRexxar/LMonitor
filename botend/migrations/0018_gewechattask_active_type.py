# Generated by Django 5.1.6 on 2025-02-21 16:13

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0017_alter_gewechattask_response'),
    ]

    operations = [
        migrations.AddField(
            model_name='gewechattask',
            name='active_type',
            field=models.IntegerField(default=0),
        ),
    ]
