from django.db import migrations


def delete_inactive_profiles(apps, schema_editor):
    SimcProfile = apps.get_model('botend', 'SimcProfile')
    SimcProfile.objects.filter(is_active=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0125_simc_multiple_backends'),
    ]

    operations = [
        migrations.RunPython(delete_inactive_profiles, migrations.RunPython.noop),
    ]