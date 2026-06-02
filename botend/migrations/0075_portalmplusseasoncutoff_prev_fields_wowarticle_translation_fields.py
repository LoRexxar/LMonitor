from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0074_remove_wowwagomonitorstate_hotfix_build_region'),
    ]

    operations = [
        migrations.AddField(
            model_name='portalmplusseasoncutoff',
            name='cutoff_0_1_prev',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='portalmplusseasoncutoff',
            name='cutoff_1_prev',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='portalmplusseasoncutoff',
            name='prev_updated_at',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wowarticle',
            name='title_cn',
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='wowarticle',
            name='content',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wowarticle',
            name='content_cn',
            field=models.TextField(blank=True, null=True),
        ),
    ]
