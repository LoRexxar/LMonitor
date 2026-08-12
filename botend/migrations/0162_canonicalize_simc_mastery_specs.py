from django.db import migrations, models


LEGACY_TO_CANONICAL_SPEC = {
    'arms': 'warrior_arms',
    'fury': 'warrior_fury',
    'protection_warrior': 'warrior_protection',
    'arcane': 'mage_arcane',
    'fire': 'mage_fire',
    'frost_mage': 'mage_frost',
    'holy_paladin': 'paladin_holy',
    'protection_paladin': 'paladin_protection',
    'retribution': 'paladin_retribution',
    'balance': 'druid_balance',
    'feral': 'druid_feral',
    'guardian': 'druid_guardian',
    'restoration_druid': 'druid_restoration',
    'blood': 'deathknight_blood',
    'frost_dk': 'deathknight_frost',
    'unholy': 'deathknight_unholy',
    'beast_mastery': 'hunter_beast_mastery',
    'marksmanship': 'hunter_marksmanship',
    'survival': 'hunter_survival',
    'discipline': 'priest_discipline',
    'holy_priest': 'priest_holy',
    'shadow': 'priest_shadow',
    'assassination': 'rogue_assassination',
    'outlaw': 'rogue_outlaw',
    'subtlety': 'rogue_subtlety',
    'elemental': 'shaman_elemental',
    'enhancement': 'shaman_enhancement',
    'restoration_shaman': 'shaman_restoration',
    'affliction': 'warlock_affliction',
    'demonology': 'warlock_demonology',
    'destruction': 'warlock_destruction',
    'brewmaster': 'monk_brewmaster',
    'windwalker': 'monk_windwalker',
    'mistweaver': 'monk_mistweaver',
    'havoc': 'demonhunter_havoc',
    'vengeance': 'demonhunter_vengeance',
    'devourer': 'demonhunter_devourer',
    'devastation': 'evoker_devastation',
    'preservation': 'evoker_preservation',
    'augmentation': 'evoker_augmentation',
}


def canonicalize_mastery_specs(apps, schema_editor):
    SimcMasteryCoefficient = apps.get_model('botend', 'SimcMasteryCoefficient')
    for legacy_spec, canonical_spec in LEGACY_TO_CANONICAL_SPEC.items():
        SimcMasteryCoefficient.objects.filter(spec=legacy_spec).update(spec=canonical_spec)


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0161_remove_mythic_route_share_code'),
    ]

    operations = [
        migrations.RunPython(canonicalize_mastery_specs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='simcmasterycoefficient',
            name='spec',
            field=models.CharField(
                help_text='规范专精标识，如 warrior_fury/mage_fire',
                max_length=50,
                unique=True,
            ),
        ),
    ]
