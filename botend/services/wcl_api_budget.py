"""One durable, non-sleeping WCL transport per OAuth client.

``graphql_request`` returns WclResult(payload, error, retry_at). Payload retains
GraphQL's {"data": ...} envelope. Only budget_wait/rate_limit carry an aware
retry_at; callers checkpoint and return to their scheduler, never retry them in
place. Reservations survive process/network failure. A fenced, expiring lease
serializes HTTP, but every ORM transaction finishes *before* HTTP starts.

Settings: WCL_BUDGET_RESERVE_FRACTION=.20, WCL_BUDGET_SYNC_SECONDS=300,
WCL_BUDGET_UNKNOWN_COST=32, WCL_BUDGET_COST_MARGIN=1.25. Unknown estimates are
conservative reservations, not claims about WCL prices. Actual limits/windows
and JSON-scalar currentRequest.total are learned from rateLimitData.
"""
import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from botend.models import WclApiBudget

URL = 'https://www.warcraftlogs.com/api/v2/client'
QUOTA_FIELDS = 'rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn currentRequest }'
QUOTA_QUERY = 'query { ' + QUOTA_FIELDS + ' }'
TIMEOUT = 30
LEASE_SECONDS = 90
# Ignore GraphQL string contents and comments when locating the root selection.
TOKENS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|#[^\n]*|[_A-Za-z][_0-9A-Za-z]*|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[^\s,]')


@dataclass(frozen=True)
class WclResult:
    payload: dict | None = None
    error: str = ''
    retry_at: object = None


class WclTransportError(Exception):
    """Safe dashboard exception: no query, credentials or upstream body."""
    def __init__(self, result):
        self.error, self.retry_at = result.error, result.retry_at
        suffix = f'; retry_at={result.retry_at.isoformat()}' if result.retry_at else ''
        super().__init__(f'WCL {result.error}{suffix}')


def account_key(client_id):
    return hashlib.sha256(str(client_id).encode('utf-8')).hexdigest()


def _setting(name, default, minimum=0, maximum=None):
    value = float(getattr(settings, name, default))
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'Invalid {name}')
    return value


def _query_info(query, variables):
    tokens = [m for m in TOKENS.finditer(query) if not m.group().startswith('#')]
    normalized = []
    parens, braces, end = 0, 0, None
    has_quota = False
    for token in tokens:
        text = token.group()
        has_quota = has_quota or text == 'rateLimitData'
        normalized.append('?' if text.startswith('"') or re.match(r'^-?\d', text) else text)
        if text == '(':
            parens += 1
        elif text == ')':
            parens -= 1
        elif not parens and text == '{':
            braces += 1
        elif not parens and text == '}':
            braces -= 1
            if braces == 0 and end is None:
                end = token.start()
    # List cardinality changes report batching cost. Values (including secrets and
    # report IDs) never enter the ledger. Scalar nullability changes query work.
    def shape(value):
        if isinstance(value, (list, tuple)):
            return '[' + ','.join(shape(item) for item in value) + ']'
        if isinstance(value, dict):
            return '{' + ','.join(str(k) + ':' + shape(v) for k, v in sorted(value.items())) + '}'
        return 'null' if value is None else type(value).__name__
    key = hashlib.sha256((' '.join(normalized) + shape(variables or {})).encode()).hexdigest()
    if end is None:
        raise ValueError('No GraphQL root selection')
    measured = query if has_quota else query[:end] + ' _wclBudget: ' + QUOTA_FIELDS + ' ' + query[end:]
    return key, has_quota, measured


def _wait(row, now, error='budget_wait'):
    candidates = [now + timedelta(seconds=1)]
    if error == 'rate_limit' and row.blocked_until:
        candidates.append(row.blocked_until)
    elif row.lease_owner and row.lease_expires_at and row.lease_expires_at > now:
        candidates.append(row.lease_expires_at)
    elif row.reset_at and row.reset_at > now:
        candidates.append(row.reset_at)
    return WclResult(error=error, retry_at=max(candidates))


def _locked(account):
    queryset = WclApiBudget.objects
    if connection.features.has_select_for_update_nowait:
        queryset = queryset.select_for_update(nowait=True)
    else:
        queryset = queryset.select_for_update()
    return queryset.get(account_key=account)


def _acquire(account, owner):
    WclApiBudget.objects.get_or_create(account_key=account)
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        if row.blocked_until and row.blocked_until > now:
            return None, _wait(row, now, 'rate_limit')
        if row.lease_owner and row.lease_expires_at and row.lease_expires_at > now:
            return None, _wait(row, now)
        # Conditional write also fences SQLite, where SELECT FOR UPDATE is a no-op.
        updated = WclApiBudget.objects.filter(pk=row.pk, lease_owner=row.lease_owner,
                                              lease_expires_at=row.lease_expires_at).update(
            lease_owner=owner, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), blocked_until=None)
        if not updated:
            return None, _wait(row, now)
        row.lease_owner, row.lease_expires_at = owner, now + timedelta(seconds=LEASE_SECONDS)
        return row, None


def _owned(row, owner, now):
    return row.lease_owner == owner and row.lease_expires_at and row.lease_expires_at > now


def _reserve(account, owner, cost, *, discovery=False):
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        if not _owned(row, owner, now):
            return _wait(row, now)
        known_window = row.limit_per_hour > 0 and row.reset_at and row.reset_at > now
        if known_window:
            available = row.limit_per_hour * (1 - _setting('WCL_BUDGET_RESERVE_FRACTION', .2, maximum=.99))
            if row.spent + cost > available:
                return WclResult(error='budget_wait', retry_at=row.reset_at)
        elif not discovery:
            return WclResult(error='budget_wait', retry_at=now + timedelta(seconds=1))
        elif row.reset_at and row.reset_at <= now:
            # Only bootstrap quota may pass an expired window. Never synthesize
            # the next reset timestamp or let a business call through first.
            row.spent = 0
        row.spent += cost
        row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        row.save(update_fields=['spent', 'lease_expires_at'])
    return None


def _quota(data):
    if not isinstance(data, dict):
        return None
    for value in [data.get('_wclBudget'), data.get('rateLimitData')]:
        if not isinstance(value, dict):
            continue
        try:
            limit, spent, reset = [float(value[k]) for k in ('limitPerHour', 'pointsSpentThisHour', 'pointsResetIn')]
            if all(math.isfinite(x) for x in (limit, spent, reset)) and limit > 0 and spent >= 0 and reset >= 0:
                current = value.get('currentRequest')
                total = float(current['total']) if isinstance(current, dict) and 'total' in current else None
                if total is not None and (not math.isfinite(total) or total < 0):
                    total = None
                return limit, spent, reset, total
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return None


def _settle(account, owner, cost_key, reserved, payload):
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        if not _owned(row, owner, now):
            return _wait(row, now)
        observed = _quota(payload.get('data')) if isinstance(payload, dict) else None
        if observed:
            limit, spent, reset, actual = observed
            row.limit_per_hour = limit
            # Real inline observations can omit some of the *current* request.
            # Add its entire measured cost: conservatively double-count at most
            # one request, instead of silently understating the upstream ledger.
            row.spent = spent + (actual if actual is not None else reserved)
            row.reset_at = now + timedelta(seconds=reset)
            row.observed_at = now
            if actual is not None:
                costs = dict(row.costs)
                costs[cost_key] = max(float(costs.get(cost_key, 0)),
                                      actual * _setting('WCL_BUDGET_COST_MARGIN', 1.25, minimum=1))
                row.costs = costs
            row.save(update_fields=['limit_per_hour', 'spent', 'reset_at', 'observed_at', 'costs'])
    return None


def _rate_limit(account, owner, headers):
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        if not _owned(row, owner, now):
            return _wait(row, now)
        candidates = [now + timedelta(seconds=1)]
        if row.reset_at and row.reset_at > now:
            candidates.append(row.reset_at)
        value = headers.get('Retry-After', '')
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                candidates.append(now + timedelta(seconds=max(0, min(seconds, 31536000))))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(value)
                if timezone.is_aware(parsed):
                    candidates.append(parsed)
            except (TypeError, ValueError, OverflowError):
                pass
        if len(candidates) == 1:
            # Unknown reset: bounded retry, explicitly NOT an assumed hour boundary.
            candidates.append(now + timedelta(seconds=60))
        row.blocked_until = max(candidates)
        row.save(update_fields=['blocked_until'])
        return WclResult(error='rate_limit', retry_at=row.blocked_until)


def _error(payload):
    errors = payload.get('errors')
    if not errors:
        return '' if isinstance(payload.get('data'), dict) else 'invalid_payload'
    for item in errors if isinstance(errors, list) else []:
        if not isinstance(item, dict):
            continue
        path = item.get('path') or []
        if ('permission to view this report' in str(item.get('message', '')).lower()
                and path[:2] == ['reportData', 'report']):
            return 'permission'
    return 'graphql'


def graphql_request(client_id, token, query, variables, *, proxies=None, post=None):
    """Return payload or a classified error; perform no sleeps or automatic retries.

    ``post`` is an optional HTTP-boundary adapter (defaults to requests.post).
    Do not call inside an outer atomic block: even a nested savepoint cannot
    release that caller's transaction before network I/O.
    """
    if not client_id or not token:
        return WclResult(error='authentication')
    if connection.in_atomic_block:
        return WclResult(error='transaction_context')
    try:
        cost_key, explicit_quota, measured_query = _query_info(query, variables)
    except (ValueError, TypeError):
        return WclResult(error='invalid_query')
    account, owner = account_key(client_id), uuid.uuid4().hex
    post = post or requests.post

    def send(q, v, key, cost, discovery=False):
        deferred = _reserve(account, owner, cost, discovery=discovery)
        if deferred:
            return deferred
        try:
            response = post(URL, json={'query': q, 'variables': v},
                            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                            timeout=TIMEOUT, proxies=proxies)
        except requests.RequestException:
            return WclResult(error='network')
        if response.status_code == 429:
            return _rate_limit(account, owner, response.headers)
        if response.status_code != 200:
            # A bare HTTP denial can be account auth or a WAF, not a private
            # report. Only the scoped GraphQL error below proves report denial.
            return WclResult(error={401: 'authentication'}.get(
                response.status_code, f'http_{response.status_code}'))
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return WclResult(error='invalid_payload')
        if not isinstance(payload, dict):
            return WclResult(error='invalid_payload')
        deferred = _settle(account, owner, key, cost, payload)
        if deferred:
            return deferred
        error = _error(payload)
        return WclResult(payload=payload if not error else None, error=error)

    acquired = False
    try:
        row, deferred = _acquire(account, owner)
        if deferred:
            return deferred
        acquired = True
        if not row.limit_per_hour or not row.reset_at or row.reset_at <= timezone.now():
            # Bootstrap exactly once, through the same lease/reservation path;
            # never recursively call graphql_request for quota discovery.
            discovery = send(QUOTA_QUERY, {}, '__quota__', float(row.costs.get('__quota__', 2)), True)
            if discovery.error:
                return discovery
            if not _quota(discovery.payload.get('data')):
                return WclResult(error='invalid_quota')
            row.refresh_from_db()
        stale = not row.observed_at or (timezone.now() - row.observed_at).total_seconds() >= _setting('WCL_BUDGET_SYNC_SECONDS', 300, minimum=1)
        measure = explicit_quota or stale or cost_key not in row.costs
        estimate = float(row.costs.get(cost_key, _setting('WCL_BUDGET_UNKNOWN_COST', 32, minimum=1)))
        if measure and cost_key in row.costs and not explicit_quota:
            estimate += float(row.costs.get('__quota__', 2))
        result = send(measured_query if measure else query, variables, cost_key, estimate)
        if result.payload and not explicit_quota:
            data = dict(result.payload['data'])
            data.pop('_wclBudget', None)
            result = WclResult(payload={**result.payload, 'data': data})
        return result
    except OperationalError:
        # NOWAIT / SQLite writer contention: let the scheduler resume, never spin.
        return WclResult(error='budget_wait', retry_at=timezone.now() + timedelta(seconds=1))
    finally:
        # Fence cleanup too: a timed-out worker cannot erase a successor's lease.
        if acquired:
            try:
                with transaction.atomic():
                    row = _locked(account)
                    if row.lease_owner == owner:
                        WclApiBudget.objects.filter(pk=row.pk, lease_owner=owner).update(
                            lease_owner='', lease_expires_at=None)
            except OperationalError:
                pass  # The bounded lease remains recoverable after DB contention.
