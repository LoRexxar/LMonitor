"""Resolve exact-build DB2 TraitCond starter grants to physical TraitNodeEntry IDs."""
from collections import defaultdict

from botend.constants.wow import SPEC_IDENTITY_MAP


DB2_GRANT_TABLES = (
    'TraitCond', 'SpecSetMember', 'TraitNodeXTraitCond',
    'TraitNodeGroupXTraitCond', 'TraitNodeGroupXTraitNode',
    'TraitNodeXTraitNodeEntry',
)


def derive_granted_entries(tables, metadata_rows):
    """Return spec ID -> Entry IDs; 0 SpecSetID means class-wide grant.

    ``metadata_rows`` defines the actual class/spec-visible entry inventory for
    this build. Never infer grants from TraitNode.Flags, position, or spell ID.
    """
    spec_sets = defaultdict(set)
    for row in tables['SpecSetMember']:
        spec_sets[int(row['SpecSet'])].add(int(row['ChrSpecializationID']))
    conditions = {}
    for row in tables['TraitCond']:
        if int(row['CondType']) == 2 and int(row['GrantedRanks']) > 0:
            spec_set_id = int(row['SpecSetID'])
            if spec_set_id and not spec_sets[spec_set_id]:
                raise ValueError(f'TraitCond {row["ID"]}: unknown SpecSetID {spec_set_id}')
            conditions[int(row['ID'])] = {0} if not spec_set_id else spec_sets[spec_set_id]

    node_specs = defaultdict(set)
    for row in tables['TraitNodeXTraitCond']:
        node_specs[int(row['TraitNodeID'])].update(conditions.get(int(row['TraitCondID']), ()))
    group_specs = defaultdict(set)
    for row in tables['TraitNodeGroupXTraitCond']:
        group_specs[int(row['TraitNodeGroupID'])].update(conditions.get(int(row['TraitCondID']), ()))
    for row in tables['TraitNodeGroupXTraitNode']:
        node_specs[int(row['TraitNodeID'])].update(group_specs[int(row['TraitNodeGroupID'])])

    entry_nodes = {
        (int(row['TraitNodeID']), int(row['TraitNodeEntryID']))
        for row in tables['TraitNodeXTraitNodeEntry']
    }
    spec_ids_by_identity = {identity: spec_id for spec_id, identity in SPEC_IDENTITY_MAP.items()}
    grants = defaultdict(set)
    for row in metadata_rows:
        identity = (row['class_name'], row['spec_name'])
        spec_id = spec_ids_by_identity.get(identity)
        if spec_id is None:
            continue
        node_id, entry_id = int(row['talent_id'] or 0), int(row['node_id'] or 0)
        starter_specs = node_specs.get(node_id, ())
        if spec_id not in starter_specs and 0 not in starter_specs:
            continue
        if (node_id, entry_id) not in entry_nodes:
            raise ValueError(f'Granted entry {entry_id} / TraitNode {node_id} absent from same-build DB2')
        grants[str(spec_id)].add(entry_id)
    return {key: sorted(entries) for key, entries in sorted(grants.items(), key=lambda pair: int(pair[0]))}
