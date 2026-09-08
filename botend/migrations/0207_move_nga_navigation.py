from django.db import migrations


OLD_URL = '/#section-nga'
NEW_URL = '/portal/nga/'


def move_navigation(apps, schema_editor):
    # The header is replaced by the navigation API after hydration.
    # Preserve administrator labels, ordering and publication choices.
    apps.get_model('botend', 'PortalNavigationItem').objects.filter(url=OLD_URL).update(url=NEW_URL)


def restore_navigation(apps, schema_editor):
    apps.get_model('botend', 'PortalNavigationItem').objects.filter(url=NEW_URL).update(url=OLD_URL)


class Migration(migrations.Migration):
    dependencies = [('botend', '0206_move_simc_baselines_navigation')]
    operations = [migrations.RunPython(move_navigation, restore_navigation)]
