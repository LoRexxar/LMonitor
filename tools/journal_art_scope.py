"""Read active snapshot files, following only slots reachable in the journal UI."""
from collections import Counter, defaultdict
from functools import lru_cache


def inventory():
    import django
    from django.conf import settings
    # Never let snapshot discovery acquire a production database connection.
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.dummy'}}
    django.setup()
    from botend.services import journal_snapshot as journal, journal_loot_snapshot as loot
    from botend.services.site_image_assets import published_image_url, published_image_map
    resolve = lru_cache(maxsize=65536)(published_image_url)
    published = published_image_map()
    if not published:
        raise ValueError('missing publication manifest')
    idx = journal.read_index()
    ids = set(idx['art_ids'])
    slots, primary, counts = defaultdict(dict), set(), Counter()
    def add(kind, i, page):
        if i:
            if type(i) != int or i not in ids:
                raise ValueError('unapproved snapshot art')
            slots[kind].setdefault(i, page)
    for card in journal.read_catalog(idx)['instances']:
        iid = card['id']
        root = f'/portal/adventure-journal/{iid}/'
        add('card', card['image'], '/portal/adventure-journal/')
        data = journal.read_instance(idx, iid)
        counts['instances'] += 1
        for d in data['instance']['difficulty_ids'] or [0]:
            bosses, _ = journal.select_boss(data, d, 0)
            if not bosses:
                continue
            for boss in bosses:
                skills = journal.read_skills(idx, data, boss, d)
                creatures = skills['boss']['creatures']
                if creatures:
                    add('portrait', creatures[0]['image'], root + f'?difficulty={d}&boss={boss["id"]}&tab=skills')
            key = loot.coordinate(idx['release']['id'], iid, d, data['instance']['source'],
                                  journal_version=data['loot_versions'][str(d)])
            # Do not call read_loot_projection: cold reads can enqueue a refresh.
            projection = loot._read_projection(key) or loot._read_legacy_reference(key)
            if projection is None:
                counts['loot_coordinates_not_displaying'] += 1
                continue
            counts['loot_coordinates_displaying'] += 1
            for row in projection['loot']:
                icon = row.get('icon')
                if not icon:
                    continue
                if resolve(row.get('icon_url')):
                    primary.add(icon)
                else:
                    add('loot', icon, root + f'?difficulty={d}&tab=loot&loot_q={row["item_id"]}')
    refs = {i: page for kind in ('loot', 'portrait', 'card') for i, page in slots[kind].items()}
    known = {i for i in ids if f'/portal/adventure-journal/art/{i}/?v=2' in published}
    missing = ids - known
    if journal.read_index()['generation'] != idx['generation']:
        raise ValueError('index changed')
    return {'generation': idx['generation'], 'allowlisted': len(ids), 'known': len(known),
            'counts': dict(counts), 'unpublished': len(missing),
            'slots': {k: {'all': len(v), 'unpublished': len(missing.intersection(v))} for k, v in slots.items()},
            'shadowed_only': sorted((missing & primary) - refs.keys()),
            'no_slot': sorted(missing - refs.keys() - primary),
            'targets': {i: refs[i] for i in sorted(missing.intersection(refs))}}


if __name__ == '__main__':
    import json
    print(json.dumps(inventory(), sort_keys=True))
