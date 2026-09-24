"""Bounded, verified Wago Hotfix search and exact-scope collection.

Wago's filter[...] parameters may be ignored. Search narrows candidates; the
inertia total, every page, source ID, and requested identity are checked.
"""

from urllib.parse import urlencode
import hashlib


def source_ids_sha256(rows):
    """Stable identity digest for one exact region/locale Hotfix interval."""
    ids = sorted(int(row['id']) for row in rows)
    return hashlib.sha256(('\n'.join(map(str, ids)) + '\n').encode('ascii')).hexdigest()



class HotfixSourceIncomplete(RuntimeError):
    pass


def _search_rows(fetch_text, extract_props, query, *, max_pages, timeout, max_attempts=3):
    """Retry unstable pagination; accept only a complete unique source-ID set."""
    max_pages = max(1, int(max_pages))
    expected_total = None
    for attempt in range(max_attempts):
        # A retry is a fresh pagination snapshot. Never combine distinct
        # incomplete attempts into an apparently complete set of source IDs.
        unique = {}
        last_page = None
        fetched = 0
        page = 1
        while page <= max_pages:
            url = 'https://wago.tools/hotfixes?' + urlencode({'search': query, 'page': page})
            text = fetch_text(url, timeout=timeout)
            props = extract_props(text)
            payload = props.get('hotfixes') if isinstance(props, dict) else None
            if not isinstance(payload, dict):
                raise HotfixSourceIncomplete(f'Hotfix incomplete query={query} page={page}: no payload')
            try:
                total = int(payload['total'])
                current = int(payload['current_page'])
                pages = int(payload['last_page'])
                per_page = int(payload['per_page'])
            except (KeyError, ValueError, TypeError):
                raise HotfixSourceIncomplete(f'Hotfix incomplete query={query} page={page}: no page metadata')
            rows = payload.get('data')
            if (not isinstance(rows, list) or current != page or pages < 1 or pages > max_pages
                    or total < 0 or per_page < 1 or len(rows) > per_page
                    or total > pages * per_page or (expected_total is not None and total != expected_total)
                    or (last_page is not None and pages != last_page)):
                raise HotfixSourceIncomplete(f'Hotfix incomplete query={query} page={page}: inconsistent pagination')
            expected_total = total
            last_page = pages
            fetched += len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    raise HotfixSourceIncomplete(f'Hotfix incomplete query={query} page={page}: invalid row')
                try:
                    source_id = int(row['id'])
                except (KeyError, ValueError, TypeError):
                    raise HotfixSourceIncomplete(f'Hotfix incomplete query={query} page={page}: missing source ID')
                if source_id <= 0:
                    raise HotfixSourceIncomplete(f'Hotfix incomplete query={query}: invalid source ID')
                prior = unique.get(source_id)
                if prior is not None and prior != row:
                    raise HotfixSourceIncomplete(f'Hotfix duplicate conflicting source ID={source_id} query={query}')
                unique[source_id] = row
            if page == pages:
                break
            page += 1
        if fetched < (expected_total or 0):
            raise HotfixSourceIncomplete(f'Hotfix incomplete query={query}: rows={fetched} total={expected_total}')
        if len(unique) == expected_total:
            return [unique[source_id] for source_id in sorted(unique)]
        if len(unique) > expected_total:
            raise HotfixSourceIncomplete(f'Hotfix inconsistent query={query}: unique IDs exceed total')
    raise HotfixSourceIncomplete(f'Hotfix duplicate/incomplete query={query}: unique={len(unique)} total={expected_total}')


def _exact_rows(rows, *, region_id, locale, table_name=None, record_id=None, push_id=None):
    selected = {}
    for row in rows:
        try:
            matched = (int(row.get('region_id') or 0) == int(region_id)
                       and str(row.get('locale') or '') == locale
                       and (push_id is None or int(row.get('push_id') or 0) == int(push_id))
                       and (record_id is None or int(row.get('record_id') or 0) == int(record_id))
                       and (table_name is None or str(row.get('table_name') or '').lower() == table_name.lower()))
        except (TypeError, ValueError):
            matched = False
        if not matched:
            continue
        table = str(row.get('table_name') or '').strip()
        try:
            rid = int(row.get('record_id') or 0)
            pid = int(row.get('push_id') or 0)
        except (TypeError, ValueError):
            rid = pid = 0
        if not table or rid <= 0 or pid <= 0:
            raise HotfixSourceIncomplete(f'Hotfix incomplete region={region_id} locale={locale}: missing record identity')
        key = (pid, table.lower(), rid)
        previous = selected.get(key)
        if previous is not None:
            raise HotfixSourceIncomplete(
                f'Hotfix duplicate record {key} region={region_id}: distinct source IDs '
                f'{previous.get("id")} and {row.get("id")}'
            )
        selected[key] = row
    return sorted(selected.values(), key=lambda row: (int(row['push_id']), int(row['id'])))


def _partition_push_rows(fetch_text, extract_props, push_id, locale, *, timeout, max_queries=512):
    """Use Wago's record-ID prefix search when its page ordering repeats IDs.

    Each split must conserve the parent's total; leaves must fit on one page.
    A change in the site's search semantics fails closed.
    """
    push_id = int(push_id)
    query_count = 0
    selected = {}

    def visit(prefix, depth):
        nonlocal query_count
        query_count += 1
        if query_count > max_queries or depth > 16:
            raise HotfixSourceIncomplete(f'Hotfix prefix search limit push={push_id}')
        query = f'{locale} {push_id}' + (f' {prefix}' if prefix else '')
        url = 'https://wago.tools/hotfixes?' + urlencode({'search': query, 'page': 1})
        props = extract_props(fetch_text(url, timeout=timeout))
        payload = props.get('hotfixes') if isinstance(props, dict) else None
        if not isinstance(payload, dict):
            raise HotfixSourceIncomplete(f'Hotfix prefix missing data query={query}')
        filters = props.get('filters') or {}
        if not isinstance(filters, dict) or filters.get('search') != query:
            raise HotfixSourceIncomplete(f'Hotfix prefix incomplete: search not applied query={query}')
        try:
            total, current, pages, per_page = (int(payload[key]) for key in
                                               ('total', 'current_page', 'last_page', 'per_page'))
        except (KeyError, ValueError, TypeError):
            raise HotfixSourceIncomplete(f'Hotfix prefix missing pagination query={query}')
        rows = payload.get('data')
        if (not isinstance(rows, list) or current != 1 or pages < 1 or per_page < 1
                or total < 0 or len(rows) > per_page or total > pages * per_page):
            raise HotfixSourceIncomplete(f'Hotfix prefix inconsistent pagination query={query}')
        if pages == 1 and len(rows) == total:
            for row in rows:
                try:
                    record_id = int(row['record_id'])
                    source_id = int(row['id'])
                    matches = (source_id > 0 and record_id > 0
                               and str(record_id).startswith(prefix)
                               and int(row['push_id']) == push_id
                               and row['locale'] == locale)
                except (KeyError, TypeError, ValueError):
                    matches = False
                if not matches:
                    raise HotfixSourceIncomplete(f'Hotfix prefix row mismatched query={query}')
                if source_id in selected:
                    raise HotfixSourceIncomplete(f'Hotfix prefix duplicate source ID={source_id}')
                selected[source_id] = row
            return total
        if total == 0 or depth >= 16:
            raise HotfixSourceIncomplete(f'Hotfix prefix incomplete: cannot split query={query}')
        child_total = sum(visit(prefix + digit, depth + 1) for digit in '0123456789')
        if child_total != total:
            raise HotfixSourceIncomplete(
                f'Hotfix prefix totals differ query={query}: children={child_total} parent={total}'
            )
        return total

    total = visit('', 0)
    if len(selected) != total:
        raise HotfixSourceIncomplete(f'Hotfix prefix unique IDs={len(selected)} total={total} push={push_id}')
    return [selected[source_id] for source_id in sorted(selected)]


def collect_hotfix_push_rows(fetch_text, extract_props, push_id, *, region_id, locale, max_pages=40, timeout=60):
    query = f'{locale} {int(push_id)}'

    def verified_props(text):
        props = extract_props(text)
        filters = props.get('filters') or {} if isinstance(props, dict) else {}
        if not isinstance(filters, dict) or filters.get('search') != query:
            raise HotfixSourceIncomplete(f'Hotfix push search not applied query={query}')
        return props

    try:
        rows = _search_rows(fetch_text, verified_props, query, max_pages=max_pages, timeout=timeout)
    except HotfixSourceIncomplete:
        rows = _partition_push_rows(fetch_text, extract_props, push_id, locale, timeout=timeout)
    return _exact_rows(rows, region_id=region_id, locale=locale, push_id=push_id)


def collect_hotfix_record_history(fetch_text, extract_props, record_id, *, table_name, region_id, locale, max_pages=40, timeout=60):
    rows = _search_rows(fetch_text, extract_props, str(int(record_id)), max_pages=max_pages, timeout=timeout)
    return _exact_rows(rows, region_id=region_id, locale=locale, table_name=table_name, record_id=record_id)


def collect_hotfix_build_rows(fetch_text, extract_props, build_number, *, region_id, locale, max_pages=200, timeout=60):
    """Discover a build's latest regional push only after its search is exhaustive."""
    build_number = int(build_number)
    rows = _search_rows(fetch_text, extract_props, str(build_number), max_pages=max_pages, timeout=timeout)
    selected = _exact_rows(rows, region_id=region_id, locale=locale)
    return [row for row in selected if str(row.get('build') or '') == str(build_number)]
