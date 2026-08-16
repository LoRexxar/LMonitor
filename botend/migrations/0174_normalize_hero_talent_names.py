from django.db import migrations


LEGACY_NAMES = {
    '奥达奇掠夺者': '奥达奇收割者',
    '巨像': '巨神兵',
    '邪痕者': '邪痕枭雄',
    '太阳使者': '烈日先驱',
    '圣光匠': '铸光者',
    '兽群领袖': '猎群领袖',
    '法术投射者': '疾咒师',
    'Annihilator': '歼灭者',
    'Void-Scarred': '虚痕枭雄',
}


def normalize_mutable_talent_resources(apps, schema_editor):
    """只修正可变资源；冻结版本和 Benchmark 结果必须保留原始 seal 输入。"""
    SimcTalentString = apps.get_model('botend', 'SimcTalentString')
    changed = 0
    for talent in SimcTalentString.objects.only('id', 'hero_talent_names').iterator():
        original = talent.hero_talent_names
        if not isinstance(original, list):
            continue
        normalized = []
        for name in original:
            title = LEGACY_NAMES.get(str(name or '').strip(), str(name or '').strip())
            if title and title not in normalized:
                normalized.append(title)
        if normalized != original:
            SimcTalentString.objects.filter(pk=talent.pk).update(
                hero_talent_names=normalized,
            )
            changed += 1
    print(f'英雄天赋历史名称规范化：更新可变天赋字符串 {changed} 条')


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0173_freeze_benchmark_result_hero_talents'),
    ]

    operations = [
        migrations.RunPython(normalize_mutable_talent_resources, migrations.RunPython.noop),
    ]
