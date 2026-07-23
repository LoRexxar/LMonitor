from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0121_delete_simcaplkeywordpair')]

    operations = [
        migrations.AddField(
            model_name='simcaplsymbol', name='trait_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='simcaplsymbol',
            index=models.Index(
                fields=['simc_revision', 'trait_id'],
                name='simc_sym_rev_trait_idx',
            ),
        ),
    ]