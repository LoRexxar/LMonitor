# Generated by Django 5.1.6 on 2025-02-21 14:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0016_gewechatauth_gewechatroomlist_gewechattask'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gewechattask',
            name='response',
            field=models.TextField(null=True),
        ),
    ]
