from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('botend', '0104_simc_default_player_template_type')]

    operations = [
        migrations.RemoveConstraint(
            model_name='simccontenttemplate',
            name='uniq_simc_default_player_source_spec',
        ),
    ]
