"""Resolve a talent icon from its exact-build TraitNodeEntry identity."""
import re


def _positive_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def resolve_talent_entry_icon(entry_id, entry_definitions, definitions, spell_misc, icon_names):
    """Return (FileDataID, CDN slug); unresolved slug remains empty.

    ``SpellIconFileDataID`` is the normal talent art. ``ActiveIconFileDataID``
    is a distinct active-state image, usable only if the normal ID is zero.
    A named override belongs to the exact entry, never its sibling TraitNode.
    """
    definition = definitions.get(_positive_int(entry_definitions.get(entry_id))) or {}
    if not definition:
        return 0, ''
    file_id = _positive_int(definition.get('OverrideIcon'))
    if not file_id:
        for field in ('VisibleSpellID', 'SpellID', 'OverridesSpellID'):
            spell_id = _positive_int(definition.get(field))
            misc = spell_misc.get(spell_id) if spell_id else None
            if not misc:
                continue
            file_id = (_positive_int(misc.get('SpellIconFileDataID'))
                       or _positive_int(misc.get('ActiveIconFileDataID')))
            if file_id:
                break
    slug = re.sub(r'\s+', '', str(icon_names.get(file_id) or '').strip()).lower()
    return file_id, slug
