from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0054_delete_portalcache"),
    ]

    operations = [
        migrations.AddField(
            model_name="wowarticle",
            name="reply_count",
            field=models.IntegerField(default=0),
        ),
    ]

