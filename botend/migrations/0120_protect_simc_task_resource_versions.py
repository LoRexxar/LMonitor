from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0119_simcapl_publication_state')]
    operations = [
        migrations.AlterField(
            model_name='simctask', name='profile_version',
            field=models.ForeignKey(blank=True, help_text='Profile版本快照', null=True,
                                    on_delete=models.PROTECT, related_name='profile_tasks',
                                    to='botend.simcresourceversion')),
        migrations.AlterField(
            model_name='simctask', name='template_version',
            field=models.ForeignKey(blank=True, help_text='Template版本快照', null=True,
                                    on_delete=models.PROTECT, related_name='template_tasks',
                                    to='botend.simcresourceversion')),
        migrations.AlterField(
            model_name='simctask', name='apl_version',
            field=models.ForeignKey(blank=True, help_text='APL版本快照', null=True,
                                    on_delete=models.PROTECT, related_name='apl_tasks',
                                    to='botend.simcresourceversion')),
    ]
