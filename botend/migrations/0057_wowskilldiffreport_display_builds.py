from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0056_wowskilldiffreport_content_html_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="wowskilldiffreport",
            name="display_from_build",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="wowskilldiffreport",
            name="display_to_build",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]

