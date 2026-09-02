"""Authoritative SimulationCraft specialization sets used by backend workflows."""

SIMC_KNOWN_SPECS = {
    'deathknight': {'blood', 'frost', 'unholy'},
    'demonhunter': {'devourer', 'havoc', 'vengeance'},
    'druid': {'balance', 'feral', 'guardian', 'restoration'},
    'evoker': {'augmentation', 'devastation', 'preservation'},
    'hunter': {'beast_mastery', 'marksmanship', 'survival'},
    'mage': {'arcane', 'fire', 'frost'},
    'monk': {'brewmaster', 'mistweaver', 'windwalker'},
    'paladin': {'holy', 'protection', 'retribution'},
    'priest': {'discipline', 'holy', 'shadow'},
    'rogue': {'assassination', 'outlaw', 'subtlety'},
    'shaman': {'elemental', 'enhancement', 'restoration'},
    'warlock': {'affliction', 'demonology', 'destruction'},
    'warrior': {'arms', 'fury', 'protection'},
}

SIMC_MID1_UNSUPPORTED_PROFILE_SPECS = {
    ('druid', 'restoration'), ('evoker', 'augmentation'),
    ('evoker', 'preservation'), ('monk', 'mistweaver'),
    ('paladin', 'holy'), ('priest', 'discipline'),
    ('priest', 'holy'), ('shaman', 'restoration'),
}

SIMC_REQUIRED_PROFILE_SPECS = {
    (class_name, spec)
    for class_name, specs in SIMC_KNOWN_SPECS.items()
    for spec in specs
} - SIMC_MID1_UNSUPPORTED_PROFILE_SPECS
