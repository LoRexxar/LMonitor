from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0053_merge_20260511_1030"),
    ]

    operations = [
        migrations.DeleteModel(
            name="PortalCache",
        ),
    ]

