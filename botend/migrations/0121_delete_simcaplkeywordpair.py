from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('botend', '0120_protect_simc_task_resource_versions')]

    operations = [
        migrations.DeleteModel(name='SimcAplKeywordPair'),
    ]