from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0073_wowwagomonitorstate_hotfix_region'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='wowwagomonitorstate',
            name='hotfix_build',
        ),
        migrations.RemoveField(
            model_name='wowwagomonitorstate',
            name='hotfix_region',
        ),
    ]

