"""Read-only imported-title terms; game names stay in central localization."""
import re

from botend.services import wow_localization


_FURY_GUIDE_KEY = re.compile(
    r'guide:(?:icy-veins|maxroll):[1-9][0-9]*:warrior_fury:[0-9a-f]{64}')
_RB = re.compile(r'(?<![A-Za-z0-9_])RB(?![A-Za-z0-9_])')


def localize_imported_title_terms(rows):
    """Mutate only display_name, returning the same materialized dict list.

    Run after the title projection's source verification. This additional fence
    excludes custom resources and noncanonical specs; it does not reload guides.
    RB is a Fury-scoped abbreviation for spell 85288, not a translated name table.
    """
    eligible = [row for row in rows
                if row.get('spec') == 'warrior_fury'
                and isinstance(row.get('system_key'), str)
                and _FURY_GUIDE_KEY.fullmatch(row['system_key'])
                and isinstance(row.get('display_name'), str)
                and _RB.search(row['display_name'])]
    if not eligible:
        return rows

    names = wow_localization.effective_names(
        'retail', class_name='Warrior', spec_name='Fury',
        reference_ids={'spell': {85288}})
    translations = set()
    for name in names:
        # Native talent records carry the spell relation; their object_id is a
        # node ID. Supplemental spell records instead use the typed object_id.
        same_spell = (
            name.get('kind') == 'spell' and name.get('object_id') == 85288
        ) or (
            name.get('kind') == 'talent'
            and 85288 in (name.get('spell_id'), name.get('display_spell_id'))
        )
        if same_spell and name.get('name_en') == 'Raging Blow':
            translation = (name.get('name_zh') or '').strip()
            if re.search(r'[\u3400-\u9fff]', translation):
                translations.add(translation)
    if len(translations) == 1:
        translation = translations.pop()
        for row in eligible:
            row['display_name'] = _RB.sub(lambda match: translation, row['display_name'])
    return rows
