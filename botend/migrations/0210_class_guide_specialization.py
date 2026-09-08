"""攻略必须绑定全站标准职业专精，回填旧文章并加数据库约束。"""
from functools import reduce
from operator import or_
from django.db import migrations, models

# 固定迁移时的目录，历史迁移不依赖今后的运行时代码。
IDENTITIES = {62: ('Mage', 'Arcane'), 63: ('Mage', 'Fire'), 64: ('Mage', 'Frost'), 65: ('Paladin', 'Holy'), 66: ('Paladin', 'Protection'), 70: ('Paladin', 'Retribution'), 71: ('Warrior', 'Arms'), 72: ('Warrior', 'Fury'), 73: ('Warrior', 'Protection'), 102: ('Druid', 'Balance'), 103: ('Druid', 'Feral'), 104: ('Druid', 'Guardian'), 105: ('Druid', 'Restoration'), 250: ('DeathKnight', 'Blood'), 251: ('DeathKnight', 'Frost'), 252: ('DeathKnight', 'Unholy'), 253: ('Hunter', 'BeastMastery'), 254: ('Hunter', 'Marksmanship'), 255: ('Hunter', 'Survival'), 256: ('Priest', 'Discipline'), 257: ('Priest', 'Holy'), 258: ('Priest', 'Shadow'), 259: ('Rogue', 'Assassination'), 260: ('Rogue', 'Outlaw'), 261: ('Rogue', 'Subtlety'), 262: ('Shaman', 'Elemental'), 263: ('Shaman', 'Enhancement'), 264: ('Shaman', 'Restoration'), 265: ('Warlock', 'Affliction'), 266: ('Warlock', 'Demonology'), 267: ('Warlock', 'Destruction'), 268: ('Monk', 'Brewmaster'), 269: ('Monk', 'Windwalker'), 270: ('Monk', 'Mistweaver'), 577: ('DemonHunter', 'Havoc'), 581: ('DemonHunter', 'Vengeance'), 1467: ('Evoker', 'Devastation'), 1468: ('Evoker', 'Preservation'), 1473: ('Evoker', 'Augmentation'), 1480: ('DemonHunter', 'Devourer')}

def bind_existing(apps, schema_editor):
    Guide = apps.get_model('botend', 'ClassGuide')
    alias = schema_editor.connection.alias
    compact = lambda value: ''.join(c for c in value.lower() if c.isalnum())
    lookup = {(compact(c), compact(s)): (key, c, s) for key, (c, s) in IDENTITIES.items()}
    rows = list(Guide.objects.using(alias).all())
    invalid = [g.pk for g in rows if (compact(g.class_name), compact(g.spec_name)) not in lookup]
    if invalid:
        raise RuntimeError(f'攻略专精无法确认，请先校订文章：{invalid}')
    for guide in rows:
        spec_id, cls, spec = lookup[(compact(guide.class_name), compact(guide.spec_name))]
        Guide.objects.using(alias).filter(pk=guide.pk).update(spec_id=spec_id, class_name=cls, spec_name=spec)

class Migration(migrations.Migration):
    dependencies = [('botend', '0209_class_guide_tags')]
    operations = [
        migrations.AddField(model_name='classguide', name='spec_id', field=models.PositiveIntegerField(null=True, db_index=True, verbose_name='专精编号')),
        migrations.RunPython(bind_existing, migrations.RunPython.noop),
        migrations.AlterField(model_name='classguide', name='spec_id', field=models.PositiveIntegerField(db_index=True, verbose_name='专精编号')),
        migrations.AddConstraint(model_name='classguide', constraint=models.CheckConstraint(
            condition=reduce(or_, (models.Q(spec_id=key, class_name=pair[0], spec_name=pair[1]) for key, pair in IDENTITIES.items())),
            name='guide_spec_identity_valid')),
    ]
