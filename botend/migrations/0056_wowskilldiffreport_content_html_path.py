from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0055_wowarticle_reply_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="wowskilldiffreport",
            name="content_html_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]

