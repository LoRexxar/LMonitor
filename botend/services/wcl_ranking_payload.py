"""Compact representation of WCL ranking selections, without discarding facts.

Used at the persistence/comparison boundary only. Parsers and consumers retain
compatibility with existing full dictionaries; unchanged rows are not migrated.
"""


def compact_ranking_talents(talents):
    if not isinstance(talents, list):
        return talents
    result = []
    for original in talents:
        if not isinstance(original, dict) or original.get('source') not in (
            'wcl_log', 'wcl_combatantinfo'
        ):
            result.append(original)
            continue
        node = dict(original)
        # Keep entry_id (canonical), talentID (legacy readers/management SQL),
        # and node_id (metadata dependency discovery). Conflicts stay visible.
        for redundant, retained in (('talent_id', 'talentID'), ('spellID', 'spell_id'), ('rank', 'points')):
            if redundant == 'talent_id' and (
                'node_id' not in node or type(node['node_id']) is not type(node.get('talent_id'))
                or node['node_id'] != node.get('talent_id')
            ):
                continue  # Keep the dependency scanner's Entry reference.
            if (redundant in node and retained in node
                    and type(node[redundant]) is type(node[retained])
                    and node[redundant] == node[retained]):
                del node[redundant]
        for key in ('spell_id', 'spellID', 'db2_trait_node_id', 'row', 'column'):
            if key in node and node[key] is None:
                del node[key]
        for key in ('name', 'icon'):
            if key in node and node[key] == '':
                del node[key]
        # Never reorder entries, invent Spell IDs, drop invalid markers, or
        # discard zero-valued ranks/coordinates and unknown source extensions.
        result.append(node)
    return result
