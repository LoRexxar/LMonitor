from django.db import migrations


def align_navigation_categories(apps, schema_editor):
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    PortalNavigationItem.objects.filter(group__key='today').update(show_in_header=False)
    PortalNavigationItem.objects.filter(
        group__key='mythic',
        show_in_home_guide=True,
    ).update(show_in_header=True)


def restore_navigation_categories(apps, schema_editor):
    PortalNavigationItem = apps.get_model('botend', 'PortalNavigationItem')
    PortalNavigationItem.objects.filter(group__key='today').update(show_in_header=True)
    PortalNavigationItem.objects.filter(group__key='mythic').update(show_in_header=False)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0199_refine_portal_navigation'),
    ]

    operations = [
        migrations.RunPython(align_navigation_categories, restore_navigation_categories),
    ]
