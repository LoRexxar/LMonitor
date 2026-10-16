"""业务分支与上游技能资料标识的唯一映射。"""


def data_branch(value='retail'):
    value = str(value or 'retail').strip().lower()
    aliases = {'retail': 'retail', 'wow': 'retail', 'live': 'retail',
               'ptr': 'ptr', 'wowxptr': 'ptr', 'wowt': 'ptr', 'ptr-2': 'ptr',
               'beta': 'beta', 'wow_beta': 'beta'}
    if value not in aliases:
        raise ValueError(f'无法识别的数据分支：{value}')
    return aliases[value]


def spell_branch(value='retail'):
    return {'retail': 'wow', 'ptr': 'wowxptr', 'beta': 'wow_beta'}[data_branch(value)]


def spell_branch_aliases(value='retail'):
    branch = data_branch(value)
    return {'retail': ('wow', 'retail'), 'ptr': ('wowxptr', 'ptr', 'wowt'),
            'beta': ('wow_beta', 'beta')}[branch]
