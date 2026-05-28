from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0071_wowwagomonitorstate_hotfix_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowwagomonitorstate',
            name='hotfix_region_id',
            field=models.IntegerField(default=0),
        ),
        migrations.AddIndex(
            model_name='wowwagomonitorstate',
            index=models.Index(fields=['hotfix_region_id'], name='wow_wago_mo_hotfix_9a1c2b_idx'),
        ),
    ]

