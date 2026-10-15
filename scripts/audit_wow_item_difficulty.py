"""Reproducible local DB2/independent-tooltip proof; no ORM writes."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts, difficulty_drop_metadata

BUILD = '12.1.0.69587'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-dir', type=Path, default=Path('/home/ubuntu/journal-difficulty-audit'))
    parser.add_argument('--instance-id', type=int, help='authoritative JournalInstance.ID for central metadata')
    args = parser.parse_args()
    records = {}
    for path in sorted(args.audit_dir.glob('*.json')):
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not all(k in data for k in ('build', 'table', 'filter', 'total', 'rows')):
            continue
        if data['build'] != BUILD or len(data['filter']) != 1:
            continue
        field, key = next(iter(data['filter'].items()))
        assert len(data['rows']) == data['total']
        assert len({r['ID'] for r in data['rows']}) == data['total']
        identity = data['table'], field, key
        if identity in records:
            assert records[identity]['rows'] == data['rows']
        records[identity] = data
    fixture = ROOT / 'botend/tests/fixtures/wow_item_difficulty_item250243_69587.json'
    fixture.write_text(json.dumps(list(records.values()), indent=2))
    def source(table, field, key):
        return records.get((table, field, key))
    results = []
    for ctx, label in ((1, 'normal'), (2, 'heroic_candidate'), (23, 'mythic_candidate')):
        result = resolve_difficulty_bonus_facts(250243, ctx, BUILD, tables=source)
        assert result['status'] == 'resolved', result['unresolved']
        independent = json.loads((args.audit_dir / f'250243-{label}-tooltip.json').read_text())
        assert 'ilvl=' not in independent['url']
        payload = independent['payload']
        match = re.search(r'<!--ilvl-->(\d+)', payload['tooltip'])
        assert match is not None, 'independent tooltip lacks item level'
        level = int(match.group(1))
        assert (level, payload['quality']) == (result['item_level'], result['quality'])
        result['independent_check'] = dict(provider='wowhead', source_scope='unversioned LIVE',
            stats_build=None, url=independent['url'], item_level=level, quality=payload['quality'],
            matches=True)
        result['provenance'] = dict(provider='wago', rule_build=BUILD,
            native_semantics='TrinityCore Item.cpp:3181-3206,2323-2332',
            group_flags='WoWDBDefs meta/flags/ItemBonusListGroupEntryFlags.dbdf',
            group_rule_scope='unique accessible no-cost entry; not a general points-mod evaluator')
        if args.instance_id:
            result['metadata'] = dict(difficulty_drop=difficulty_drop_metadata(result,
                                      difficulty_id=ctx, instance_id=args.instance_id))
        results.append(result)
        print(json.dumps({k: result[k] for k in ('item_context', 'status', 'bonus_ids', 'item_level', 'quality', 'level_evaluation', 'independent_check')}, ensure_ascii=False))
    report = args.audit_dir / 'strict-difficulty-item250243-by-context.json'
    report.write_text(json.dumps(results, indent=2))
    print('verified_envelopes', len(records), 'report', report)

if __name__ == '__main__':
    main()
