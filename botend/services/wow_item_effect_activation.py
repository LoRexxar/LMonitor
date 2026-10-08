"""Bounded, exact-build DB2 activation facts; no catalog writes or SimC policy.

Inject the existing gear catalog source (CurrentGearCatalogSource at this
revision). Its session/proxies/timeouts and _get remain the only HTTP transport.
Only fully verified Type=23 bonus -> ItemEffect chains produce required bonuses.
Unresolved choices are evidence, never a union of all reachable bonus lists.
"""
from __future__ import annotations

import html
import json
import re
from collections import deque
from urllib.parse import parse_qs, urlencode, urlsplit

WAGO_DB2_ROOT = 'https://wago.tools/db2'
SCHEMA_VERSION = 1
LOCALES = frozenset(('enUS', 'enGB', 'zhCN', 'zhTW', 'deDE', 'esES', 'esMX',
                     'frFR', 'itIT', 'koKR', 'ptBR', 'ruRU'))
NODE_GUARDS = (
    'ItemContext', 'ChildItemLevelSelectorID', 'ChildItemBonusListGroupID',
    'IblGroupPointsModSetID', 'MinMythicPlusLevel', 'MaxMythicPlusLevel',
    'ItemCreationContextGroupID', 'Flags',
)
NODE_EDGES = ('ChildItemBonusTreeID', 'ChildItemBonusListID')


class ActivationSourceError(RuntimeError):
    """Invalid or mismatched source response; no result may be imported."""


class _LimitReached(Exception):
    pass


def _integer(value, field, *, minimum=0):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value):
        result = int(value)
    else:
        raise ActivationSourceError(f'Invalid integer {field}: {value!r}')
    if result < minimum:
        raise ActivationSourceError(f'Invalid integer {field}: {value!r}')
    return result


def _blockers(row, *, guards, allowed):
    """Unknown nonzero fields and missing guard columns are fail-closed."""
    blockers = {}
    for key in guards:
        if key not in row or _integer(row[key], key) != 0:
            blockers[key] = row.get(key)
    for key, value in row.items():
        if key not in allowed and value not in (0, '0'):
            blockers[key] = value
    return blockers


class ItemEffectActivationCollector:
    """One isolated budget/cache per collect call; instance is safe to reuse.

    Limits cover the entire item, not each branch. A bounded-out query contributes
    no partially fetched rows. Identity/pagination violations raise; limits and
    missing references produce unresolved_paths. collection_complete means no
    unresolved path, not merely successful HTTP. Effects retain DB2 TriggerType
    without interpreting it as a simulator eligibility rule.
    """
    def __init__(self, *, source, max_depth=8, max_requests=64, max_pages=4,
                 max_rows=1000, max_response_bytes=4_000_000):
        if not callable(getattr(source, '_get', None)):
            raise TypeError('source must provide the catalog _get transport')
        self.source = source
        self.limits = {
            'max_depth': _integer(max_depth, 'max_depth'),
            'max_requests': _integer(max_requests, 'max_requests', minimum=1),
            'max_pages': _integer(max_pages, 'max_pages', minimum=1),
            'max_rows': _integer(max_rows, 'max_rows', minimum=1),
            'max_response_bytes': _integer(max_response_bytes, 'max_response_bytes', minimum=1),
        }
        # Hard ceilings also bound CPU/recursion and user-supplied budgets.
        for key, ceiling in {'max_depth': 32, 'max_requests': 256, 'max_pages': 16,
                             'max_rows': 10000, 'max_response_bytes': 8_000_000}.items():
            if self.limits[key] > ceiling:
                raise ActivationSourceError(f'{key} exceeds hard ceiling {ceiling}')

    def collect(self, *, item_id, game_build, locale='enUS'):
        item_id = _integer(item_id, 'item_id', minimum=1)
        if not isinstance(game_build, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', game_build):
            raise ActivationSourceError('game_build must be an exact dotted build')
        if locale not in LOCALES:
            raise ActivationSourceError('Unsupported DB2 locale')
        return _Collection(self.source, self.limits, item_id, game_build, locale).run()


class _Collection:
    def __init__(self, source, limits, item_id, build, locale):
        self.source, self.limits = source, limits
        self.item_id, self.build, self.locale = item_id, build, locale
        self.requests = self.row_count = 0
        self.cache, self.evidence, self.unresolved = {}, [], []
        self.effects, self.required = {}, set()

    def _unresolved(self, reason, path, *, row=None, **details):
        self.unresolved.append({'reason': reason, 'path': path, **details,
                                **({'row': row} if row is not None else {})})

    def _url(self, table, field, value, page=1):
        params = {'build': self.build, 'locale': self.locale,
                  f'filter[{field}]': f'exact:{value}'}
        if page != 1:
            params['page'] = page
        return f'{WAGO_DB2_ROOT}/{table}?' + urlencode(params)

    def _validate_url(self, url, table, field, value, page):
        if not isinstance(url, str):
            raise ActivationSourceError('Missing DB2 response/pagination URL')
        parts = urlsplit(url)
        expected = urlsplit(WAGO_DB2_ROOT)
        if (parts.scheme, parts.netloc, parts.path, parts.fragment) != (
                expected.scheme, expected.netloc, f'{expected.path}/{table}', ''):
            raise ActivationSourceError(f'Wrong DB2 response/pagination endpoint: {url}')
        query = parse_qs(parts.query, keep_blank_values=True)
        wanted = {'build': [self.build], 'locale': [self.locale],
                  f'filter[{field}]': [f'exact:{value}']}
        if 'page' in query or page != 1:
            wanted['page'] = [str(page)]
        if query != wanted:
            raise ActivationSourceError(f'Wrong DB2 response/pagination query: {url}')

    def _page(self, table, field, value, number):
        if self.requests >= self.limits['max_requests']:
            raise _LimitReached('request_limit')
        url = self._url(table, field, value, number)
        self.requests += 1
        # No Inertia headers, new session, mirror, or retry outside the budget.
        response = self.source._get(url)
        self._validate_url(response.url, table, field, value, number)
        text = response.text
        if len(text.encode('utf-8')) > self.limits['max_response_bytes']:
            raise _LimitReached('response_size_limit')
        match = re.search(r'data-page=(?:"([^"]+)"|\'([^\']+)\')', text)
        if not match:
            raise ActivationSourceError(f'Missing Wago data-page: {url}')
        try:
            page = json.loads(html.unescape(match.group(1) or match.group(2)))
            props = page['props']
            expected_filters = {'build': self.build, 'locale': self.locale,
                                'filter': {field: f'exact:{value}'}}
            filters = props['filters']
            # Wago may echo the current page in filters, but nothing else.
            if 'page' in filters:
                if _integer(filters['page'], 'filters.page', minimum=1) != number:
                    raise ActivationSourceError('Wrong filters.page')
                filters = {k: v for k, v in filters.items() if k != 'page'}
            if (props['currentTable'] != table or props['currentVersion'] != self.build
                    or filters != expected_filters):
                raise ActivationSourceError(f'DB2 table/build/locale/filter mismatch: {url}')
            data = props['data']
            rows = data['data']
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ActivationSourceError('Invalid paginator rows')
            current = _integer(data['current_page'], 'current_page', minimum=1)
            total = _integer(data['total'], 'total')
            last = _integer(data['last_page'], 'last_page', minimum=1)
            per_page = _integer(data['per_page'], 'per_page', minimum=1)
            if (current != number or last != max(1, (total + per_page - 1) // per_page)
                    or number > last):
                raise ActivationSourceError('Inconsistent DB2 paginator metadata')
            count = min(per_page, max(0, total - (number - 1) * per_page))
            if len(rows) != count:
                raise ActivationSourceError('DB2 paginator total disagrees with returned rows')
            start = (number - 1) * per_page + 1 if count else None
            end = (number - 1) * per_page + count if count else None
            if data['from'] != start or data['to'] != end or data['path'] != f'{WAGO_DB2_ROOT}/{table}':
                raise ActivationSourceError('Inconsistent DB2 paginator range/path')
            for key, target in (('first_page_url', 1), ('last_page_url', last),
                                ('prev_page_url', number - 1 if number > 1 else None),
                                ('next_page_url', number + 1 if number < last else None)):
                if target is None:
                    if data[key] is not None:
                        raise ActivationSourceError(f'Unexpected {key}')
                else:
                    self._validate_url(data[key], table, field, value, target)
            ids = []
            for row in rows:
                ids.append(_integer(row['ID'], 'ID', minimum=1))
                if _integer(row[field], field) != value:
                    raise ActivationSourceError(f'Wrong {table}.{field} returned row identity')
            if len(set(ids)) != len(ids):
                raise ActivationSourceError('Duplicate DB2 row IDs within page')
        except (KeyError, TypeError, ValueError) as exc:
            raise ActivationSourceError(f'Malformed Wago DB2 response: {url}') from exc
        remaining_query_rows = total if number == 1 else len(rows)
        if self.row_count + remaining_query_rows > self.limits['max_rows']:
            raise _LimitReached('row_limit')
        if last > self.limits['max_pages']:
            raise _LimitReached('page_limit')
        self.evidence.append({'table': table, 'url': response.url,
                              'game_build': self.build, 'locale': self.locale,
                              'filters': expected_filters, 'rows': rows,
                              'pagination': {k: v for k, v in data.items() if k != 'data'}})
        self.row_count += len(rows)
        return rows, (total, last, per_page)

    def _rows(self, table, field, value, path):
        key = table, field, value
        if key in self.cache:
            return self.cache[key]
        rows, identity = [], None
        try:
            number = 1
            while True:
                batch, meta = self._page(table, field, value, number)
                if identity is not None and identity != meta:
                    raise ActivationSourceError('DB2 pagination total/size changed between pages')
                identity = meta
                rows.extend(batch)
                if number == meta[1]:
                    break
                number += 1
            if len(rows) != identity[0] or len({_integer(r['ID'], 'ID', minimum=1) for r in rows}) != len(rows):
                raise ActivationSourceError('Incomplete or duplicate DB2 pagination rows')
        except _LimitReached as exc:
            self._unresolved(str(exc), path, table=table, url=self._url(table, field, value))
            return None
        self.cache[key] = rows
        return rows

    def _effect(self, effect_id, path, bonus_id=None):
        rows = self._rows('ItemEffect', 'ID', effect_id, path)
        if rows is None:
            return
        if len(rows) != 1:
            self._unresolved('missing_item_effect', path, item_effect_id=effect_id)
            return
        row = rows[0]
        guards = ('PlayerConditionID', 'ChrSpecializationID')
        allowed = {'ID', 'SpellID', 'TriggerType', 'LegacySlotIndex', 'Charges',
                   'CoolDownMSec', 'CategoryCoolDownMSec', 'SpellCategoryID', *guards}
        blockers = _blockers(row, guards=guards, allowed=allowed)
        if blockers:
            self._unresolved('conditional_item_effect', path, row=row, blockers=blockers)
            return
        fact = {'item_effect_id': effect_id,
                'spell_id': _integer(row.get('SpellID'), 'SpellID', minimum=1),
                'trigger_type': _integer(row.get('TriggerType'), 'TriggerType')}
        if bonus_id is not None:
            fact['bonus_id'] = bonus_id
            self.required.add(bonus_id)
        self.effects[(effect_id, bonus_id)] = fact

    def _bonus(self, bonus_id, path):
        rows = self._rows('ItemBonus', 'ParentItemBonusListID', bonus_id, path)
        if rows is None:
            return
        if not rows:
            self._unresolved('missing_item_bonus', path, bonus_id=bonus_id)
        for row in rows:
            if _integer(row.get('Type'), 'Type') != 23:
                continue
            guards = ('Value_1', 'Value_2', 'Value_3')
            blockers = _blockers(row, guards=guards, allowed={
                'ID', 'ParentItemBonusListID', 'Type', 'Value_0', 'OrderIndex', *guards})
            bonus_path = [*path, {'table': 'ItemBonus', 'row_id': row['ID'], 'bonus_id': bonus_id}]
            if blockers:
                self._unresolved('unsupported_item_bonus', bonus_path, row=row, blockers=blockers)
                continue
            self._effect(_integer(row.get('Value_0'), 'Value_0', minimum=1), bonus_path, bonus_id)

    def run(self):
        root_path = [{'table': 'Item', 'item_id': self.item_id}]
        roots = self._rows('ItemXBonusTree', 'ItemID', self.item_id, root_path)
        queue, expanded = deque(), set()
        for row in roots or []:
            path = [*root_path, {'table': 'ItemXBonusTree', 'row_id': row['ID']}]
            blockers = _blockers(row, guards=(), allowed={'ID', 'ItemID', 'ItemBonusTreeID'})
            if blockers:
                self._unresolved('conditional_tree_link', path, row=row, blockers=blockers)
            else:
                queue.append((_integer(row.get('ItemBonusTreeID'), 'ItemBonusTreeID', minimum=1),
                              path, (), 0))
        while queue:
            tree_id, path, ancestors, depth = queue.popleft()
            if tree_id in ancestors:
                self._unresolved('cycle', path, tree_id=tree_id)
                continue
            if tree_id in expanded:
                continue
            if depth > self.limits['max_depth']:
                self._unresolved('depth_limit', path, tree_id=tree_id)
                continue
            expanded.add(tree_id)
            rows = self._rows('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree_id, path)
            if rows is None:
                continue
            if not rows:
                self._unresolved('missing_tree_nodes', path, tree_id=tree_id)
            for row in rows:
                node_path = [*path, {'table': 'ItemBonusTreeNode', 'row_id': row['ID'], 'tree_id': tree_id}]
                blockers = _blockers(row, guards=NODE_GUARDS,
                                     allowed={'ID', 'ParentItemBonusTreeID', *NODE_EDGES, *NODE_GUARDS})
                if blockers:
                    self._unresolved('conditional_or_unsupported_node', node_path, row=row, blockers=blockers)
                    continue
                child = _integer(row.get('ChildItemBonusTreeID'), 'ChildItemBonusTreeID')
                bonus = _integer(row.get('ChildItemBonusListID'), 'ChildItemBonusListID')
                if bonus:
                    self._bonus(bonus, node_path)
                if child:
                    queue.append((child, node_path, (*ancestors, tree_id), depth + 1))
        direct = self._rows('ItemXItemEffect', 'ItemID', self.item_id, root_path)
        for row in direct or []:
            path = [*root_path, {'table': 'ItemXItemEffect', 'row_id': row['ID']}]
            blockers = _blockers(row, guards=(), allowed={'ID', 'ItemID', 'ItemEffectID', 'OrderIndex'})
            if blockers:
                self._unresolved('conditional_effect_link', path, row=row, blockers=blockers)
                continue
            self._effect(_integer(row.get('ItemEffectID'), 'ItemEffectID', minimum=1), path)
        # Reports show triggered buffs/damage, not necessarily a driver row.
        # Preserve the exact-build spell family without guessing by its name.
        events = set()
        spells = deque((effect['spell_id'], 0) for effect in self.effects.values())
        while spells:
            spell_id, depth = spells.popleft()
            if spell_id in events:
                continue
            if depth > self.limits['max_depth']:
                self._unresolved('spell_depth_limit', root_path, spell_id=spell_id)
                continue
            events.add(spell_id)
            spell_rows = self._rows('SpellEffect', 'SpellID', spell_id, root_path)
            for row in spell_rows or []:
                trigger = _integer(row.get('EffectTriggerSpell'), 'EffectTriggerSpell')
                if trigger and trigger not in events:
                    spells.append((trigger, depth + 1))
        return {
            'schema_version': SCHEMA_VERSION, 'item_id': self.item_id, 'game_build': self.build,
            'required_bonus_ids': sorted(self.required),
            'event_spell_ids': sorted(events),
            'effects': sorted(self.effects.values(), key=lambda e: (e['item_effect_id'], e.get('bonus_id', 0))),
            'source': {'provider': 'wago_db2', 'locale': self.locale,
                       'requests': self.requests, 'row_count': self.row_count,
                       'limits': dict(self.limits), 'evidence': self.evidence},
            'unresolved_paths': self.unresolved, 'collection_complete': not self.unresolved,
        }
