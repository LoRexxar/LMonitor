from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0247_central_spell_display'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='wowtalentnodemetadata',
            name='wow_name_reference_unique',
        ),
        migrations.AddConstraint(
            model_name='wowtalentnodemetadata',
            constraint=models.UniqueConstraint(fields=('talent_version', 'name_kind', 'reference_id'), name='wow_name_reference_branch_unique'),
        ),
    ]
