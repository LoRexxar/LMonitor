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


def _exact_rows(rows, *, region_id, locale, table_name=None, record_id=None, push_id=None, build_number=None):
    # None explicitly means all builds, not a guessed/latest build. Pagination
    # completeness is established before selecting this coordinate.
    if build_number is not None and (not str(build_number).isdigit() or int(build_number) <= 0):
        raise HotfixSourceIncomplete(f'Hotfix invalid requested build identity: {build_number}')
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
        build = row.get('build')
        if not str(build).isdigit() or int(build) <= 0:
            raise HotfixSourceIncomplete(f'Hotfix incomplete source ID={row.get("id")}: missing build identity')
        if build_number is not None and int(build) != int(build_number):
            continue
        key = (pid, int(build), table.lower(), rid)
        previous = selected.get(key)
        if previous is not None:
            raise HotfixSourceIncomplete(
                f'Hotfix duplicate record {key} region={region_id}: distinct source IDs '
                f'{previous.get("id")} and {row.get("id")}'
            )
        selected[key] = row
    return sorted(selected.values(), key=lambda row: (int(row['push_id']), int(row['id'])))


def _partition_push_rows(fetch_text, extract_props, push_id, locale, *, timeout, max_queries=512,
                         checkpoint=None, proof=None, max_pages=40):
    """Use Wago's record-ID prefix search when its page ordering repeats IDs.

    Each split must conserve the parent's total; leaves must fit on one page.
    A change in the site's search semantics fails closed.
    """
    push_id = int(push_id)
    query_count = 0
    selected = {}
    leaves = []

    def fetch_bounded(url, **kwargs):
        nonlocal query_count
        query_count += 1
        if query_count > max_queries:
            raise HotfixSourceIncomplete(f'Hotfix prefix search limit push={push_id}')
        return fetch_text(url, **kwargs)

    def store(rows, prefix, build_prefix=None):
        if rows:
            leaves.append({'prefix': prefix, 'build_prefix': build_prefix, 'rows': rows})
        for row in rows:
            source_id = 0
            try:
                record_id = int(row['record_id'])
                source_id = int(row['id'])
                matches = (source_id > 0 and record_id > 0
                           and str(record_id).startswith(prefix)
                           and int(row['push_id']) == push_id
                           and row['locale'] == locale)
                if build_prefix is not None:
                    matches = (matches and str(record_id) == prefix
                               and int(row['build']) > 0
                               and str(row['build']).startswith(build_prefix))
            except (KeyError, TypeError, ValueError):
                matches = False
            if not matches:
                raise HotfixSourceIncomplete(f'Hotfix prefix row mismatched push={push_id} prefix={prefix}')
            if source_id in selected:
                raise HotfixSourceIncomplete(f'Hotfix prefix duplicate source ID={source_id}')
            selected[source_id] = row

    def search(query):
        def verified_props(text):
            props = extract_props(text)
            filters = props.get('filters') or {} if isinstance(props, dict) else {}
            if not isinstance(filters, dict) or filters.get('search') != query:
                raise HotfixSourceIncomplete(f'Hotfix terminal search not applied query={query}')
            return props
        return _search_rows(fetch_bounded, verified_props, query, max_pages=max_pages,
                            timeout=timeout, max_attempts=1)

    def terminal_rows(prefix, remaining):
        # Wago trims a trailing space. Include the next token (build) to
        # select record_id == prefix, never the longer digit descendants.
        count = 0
        for digit in '0123456789':
            rows = search(f'{locale} {push_id} {prefix} {digit}')
            store(rows, prefix, build_prefix=digit)
            count += len(rows)
            if count >= remaining:
                break
        return count

    def page_one(prefix):
        query = f'{locale} {push_id}' + (f' {prefix}' if prefix else '')
        url = 'https://wago.tools/hotfixes?' + urlencode({'search': query, 'page': 1})
        props = extract_props(fetch_bounded(url, timeout=timeout))
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
        return total, pages, rows

    def visit(prefix, depth, first=None):
        if depth > 16:
            raise HotfixSourceIncomplete(f'Hotfix prefix search limit push={push_id}')
        total, pages, rows = first if first is not None else page_one(prefix)
        query = f'{locale} {push_id}' + (f' {prefix}' if prefix else '')
        if pages == 1 and len(rows) == total:
            store(rows, prefix)
            return total
        if total == 0 or depth >= 16:
            raise HotfixSourceIncomplete(f'Hotfix prefix incomplete: cannot split query={query}')
        child_total = 0
        for digit in '0123456789':
            child_total += visit(prefix + digit, depth + 1)
            # Each child is complete, disjoint and source-ID unique. Once
            # they account for the full parent total, no rows remain to find.
            if child_total >= total:
                break
        if prefix and child_total < total:
            child_total += terminal_rows(prefix, total - child_total)
        if child_total != total:
            raise HotfixSourceIncomplete(
                f'Hotfix prefix totals differ query={query}: children={child_total} parent={total}'
            )
        return total

    root = page_one('')
    reused = False
    # There is no upstream whole-push revision/immutability contract. A root
    # page hash or TTL cannot validate unseen rows. Recheck every nonempty
    # complete leaf against the live source, then conserve the live root total.
    # This reuses the verified partition proof, skipping empty/internal probes;
    # it deliberately does NOT promise zero-HTTP reuse of an old push.
    if (isinstance(checkpoint, dict) and checkpoint.get('total') == root[0]
            and root[1] > 1 and isinstance(checkpoint.get('leaves'), list)):
        try:
            for leaf in checkpoint['leaves']:
                prefix, build_prefix = leaf['prefix'], leaf['build_prefix']
                if (not isinstance(prefix, str) or not prefix.isascii() or not prefix.isdigit()
                        or len(prefix) > 16 or (build_prefix is not None
                        and build_prefix not in tuple('0123456789'))):
                    raise HotfixSourceIncomplete('Hotfix checkpoint invalid prefix')
                cached = leaf['rows']
                if not isinstance(cached, list) or not cached:
                    raise HotfixSourceIncomplete('Hotfix checkpoint invalid leaf')
                if build_prefix is None:
                    count, pages, fresh = page_one(prefix)
                    if pages != 1 or len(fresh) != count:
                        raise HotfixSourceIncomplete('Hotfix checkpoint leaf no longer complete')
                else:
                    fresh = search(f'{locale} {push_id} {prefix} {build_prefix}')
                if sorted(fresh, key=lambda r: int(r['id'])) != sorted(cached, key=lambda r: int(r['id'])):
                    raise HotfixSourceIncomplete('Hotfix checkpoint source changed')
                store(cached, prefix, build_prefix)
            if len(selected) != root[0]:
                raise HotfixSourceIncomplete('Hotfix checkpoint root total changed')
            reused = True
        except (HotfixSourceIncomplete, KeyError, TypeError, ValueError):
            # Discard the ENTIRE attempted proof, never combine old and new
            # incomplete sets. Failed revalidation consumes the same budget.
            selected.clear()
            leaves.clear()
    total = root[0] if reused else visit('', 0, root)
    if len(selected) != total:
        raise HotfixSourceIncomplete(f'Hotfix prefix unique IDs={len(selected)} total={total} push={push_id}')
    if proof is not None:
        proof.update(total=total, leaves=leaves, reused=reused)
    return [selected[source_id] for source_id in sorted(selected)]


def collect_hotfix_push_rows(fetch_text, extract_props, push_id, *, region_id, locale, build_number=None,
                             max_pages=40, timeout=60, checkpoint=None, proof=None):
    # Avoid three doomed pagination sweeps on Wago's unstable ordering. The
    # first root page is also the single-page fast path; larger pushes split
    # immediately under the existing parent/child and unique-ID conservation.
    candidate = {}
    rows = _partition_push_rows(fetch_text, extract_props, push_id, locale, timeout=timeout,
                                max_pages=max_pages, checkpoint=checkpoint, proof=candidate)
    selected = _exact_rows(rows, region_id=region_id, locale=locale, push_id=push_id, build_number=build_number)
    if proof is not None:
        proof.update(candidate)
    return selected


def collect_hotfix_record_history(fetch_text, extract_props, record_id, *, table_name, region_id, locale, build_number=None, max_pages=40, timeout=60, search_cache=None):
    # Caller owns a single resolution's cache, never a persistent page cache.
    # All scopes search the same raw record query; filter only AFTER a complete
    # independent sweep. Failed sweeps must never be combined or treated as [].
    query = str(int(record_id))
    key = ('https://wago.tools/hotfixes', query, max_pages, timeout)
    cached = search_cache.get(key) if search_cache is not None else None
    if cached is None:
        try:
            rows = _search_rows(fetch_text, extract_props, query, max_pages=max_pages, timeout=timeout)
        except (HotfixSourceIncomplete, OSError) as exc:
            if search_cache is not None:
                search_cache[key] = ('incomplete', str(exc))
            raise
        if search_cache is not None:
            search_cache[key] = ('complete', rows)
    elif cached[0] == 'incomplete':
        raise HotfixSourceIncomplete(cached[1])
    else:
        rows = cached[1]
    # Scope conflicts do not poison the complete search for other coordinates.
    return _exact_rows(rows, region_id=region_id, locale=locale, table_name=table_name, record_id=record_id, build_number=build_number)


def collect_hotfix_build_rows(fetch_text, extract_props, build_number, *, region_id, locale, max_pages=200, timeout=60):
    """Discover a build's latest regional push only after its search is exhaustive."""
    build_number = int(build_number)
    rows = _search_rows(fetch_text, extract_props, str(build_number), max_pages=max_pages, timeout=timeout)
    return _exact_rows(rows, region_id=region_id, locale=locale, build_number=build_number)
