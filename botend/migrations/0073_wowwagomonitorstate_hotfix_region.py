from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0072_wowwagomonitorstate_hotfix_region_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_region',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddIndex(
            model_name='wowwagomonitorstate',
            index=models.Index(fields=['hotfix_region'], name='wow_wago_mo_hotfix_4f52f6_idx'),
        ),
    ]

