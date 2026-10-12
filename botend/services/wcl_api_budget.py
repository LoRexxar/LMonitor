"""Durable WCL admission with parallel HTTP and short row-locked transactions.

``spent`` is a monotonic conservative admission bound, not a refund balance.
``charged`` counts local reservations in the calibrated generation; ``completed``
counts confirmed completions. Each request captures completed-before-dispatch as
its baseline. An observation can safely raise spent to upstream_spent + charged
- baseline: every local charge not certainly included in that response survives,
even when responses finish in reverse order. No expired request may settle.

Only bootstrap/reset and unlearned query costs are single-flight. Failed charges
remain booked until authoritative reset calibration. Estimates cannot guarantee
an upper bound on arbitrary upstream prices; overruns are booked immediately.
No sleeps, automatic retries, assumed hourly limits, or HTTP inside transactions.
"""
import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from contextvars import ContextVar
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from botend.models import WclApiBudget, WclApiReservation

URL = 'https://www.warcraftlogs.com/api/v2/client'
QUOTA_FIELDS = 'rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn currentRequest }'
QUOTA_QUERY = 'query { ' + QUOTA_FIELDS + ' }'
TIMEOUT = 30
LEASE_SECONDS = 90
# Per collector batch/thread: a busy later page must not replay earlier HTTP.
business_requests_sent = ContextVar('wcl_business_requests_sent', default=0)
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


def collection_worker_limit(client_id):
    """Live collector submission ceiling, not a global HTTP mutex."""
    row, _ = WclApiBudget.objects.get_or_create(account_key=account_key(client_id))
    return max(1, min(16, int(row.collection_workers)))


def _locked(account):
    # Block only for another *short* ledger transaction. NOWAIT would turn a
    # simultaneous pool start into needless scheduler deferrals on InnoDB.
    return WclApiBudget.objects.select_for_update().get(account_key=account)


def _reserve(account, owner, cost_key, explicit_quota):
    WclApiBudget.objects.get_or_create(account_key=account)
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        if row.blocked_until and row.blocked_until > now:
            return None, None, _wait(row, now, 'rate_limit')
        if row.lease_owner and row.lease_expires_at and row.lease_expires_at > now:
            return None, None, WclResult(error='admission_busy', retry_at=now + timedelta(seconds=1))
        discovery = not row.limit_per_hour or not row.reset_at or row.reset_at <= now
        unknown = cost_key not in row.costs
        if discovery or unknown:
            active = row.reservations.filter(expires_at__gt=now).order_by('expires_at').first()
            if active:
                return None, None, WclResult(error='admission_busy', retry_at=now + timedelta(seconds=1))
        if discovery and row.reset_at and row.reset_at <= now:
            # Only after all live requests finish/expire may the reset probe
            # begin a new generation. Old completions are now fenced out.
            row.window_generation += 1
            row.spent = row.charged = row.completed = 0
            row.reset_at = None
            row.reservations.all().delete()
        stale = not row.observed_at or (now - row.observed_at).total_seconds() >= _setting('WCL_BUDGET_SYNC_SECONDS', 300, minimum=1)
        measure = explicit_quota or stale or unknown
        cost = float(row.costs.get('__quota__', 2)) if discovery else float(
            row.costs.get(cost_key, _setting('WCL_BUDGET_UNKNOWN_COST', 32, minimum=1)))
        if measure and not unknown and not explicit_quota and not discovery:
            cost += float(row.costs.get('__quota__', 2))
        if not math.isfinite(cost) or cost < 0:
            raise ValueError('Invalid WCL cost estimate')
        available = row.limit_per_hour * (1 - _setting('WCL_BUDGET_RESERVE_FRACTION', .2, maximum=.99))
        if row.limit_per_hour > 0 and row.spent + cost > available:
            # An unsuccessful reset probe must not get an unlimited allowance.
            return None, None, _wait(row, now)
        expires = now + timedelta(seconds=LEASE_SECONDS)
        if discovery or unknown:
            row.lease_owner, row.lease_expires_at = owner, expires
        else:
            row.lease_owner, row.lease_expires_at = '', None
        reservation = WclApiReservation.objects.create(
            budget=row, request_id=owner, generation=row.window_generation,
            cost=cost, baseline=row.completed, expires_at=expires, discovery=discovery)
        row.spent += cost
        row.charged += cost
        row.save(update_fields=['window_generation', 'spent', 'charged', 'completed',
                                'reset_at', 'lease_owner', 'lease_expires_at'])
        return reservation, measure, None


def _owned(row, reservation, now):
    return (reservation and reservation.generation == row.window_generation
            and reservation.expires_at > now
            and (not row.lease_owner or row.lease_owner == reservation.request_id))


def _release(row, owner):
    if row.lease_owner == owner:
        row.lease_owner, row.lease_expires_at = '', None


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


def _settle(account, owner, cost_key, payload):
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        reservation = row.reservations.filter(request_id=owner).first()
        if not _owned(row, reservation, now):
            return _wait(row, now)
        observed = _quota(payload.get('data')) if isinstance(payload, dict) else None
        if reservation.discovery and not observed:
            # Invalid bootstrap is still an uncertain paid request. Retain the
            # fence until expiry so repeated callers cannot spin free probes.
            return None
        if observed:
            limit, spent, reset, actual = observed
            if actual is not None:
                extra = max(0, actual - reservation.cost)
                row.charged += extra
                row.spent += extra
                reservation.cost += extra
                costs = dict(row.costs)
                costs[cost_key] = max(float(costs.get(cost_key, 0)),
                                      actual * _setting('WCL_BUDGET_COST_MARGIN', 1.25, minimum=1))
                row.costs = costs
            row.limit_per_hour = limit if reservation.discovery else min(row.limit_per_hour, limit)
            row.spent = max(row.spent, spent + row.charged - reservation.baseline)
            deadline = now + timedelta(seconds=reset)
            row.reset_at = deadline if reservation.discovery else min(row.reset_at, deadline)
            row.observed_at = now
        # Completion counters are causal, not upstream quota facts. Including a
        # request in future baselines is safe only after its HTTP has returned.
        row.completed += reservation.cost
        _release(row, owner)
        row.save(update_fields=['limit_per_hour', 'spent', 'charged', 'completed',
                                'reset_at', 'observed_at', 'costs', 'lease_owner', 'lease_expires_at'])
        reservation.delete()
    return None


def _abandon(account, owner):
    """Release active ownership, never refund an uncertain HTTP charge."""
    with transaction.atomic():
        row = _locked(account)
        reservation = row.reservations.filter(request_id=owner).first()
        if not reservation or reservation.generation != row.window_generation or reservation.discovery:
            return
        row.reservations.filter(pk=reservation.pk).update(expires_at=timezone.now())
        _release(row, owner)
        row.save(update_fields=['lease_owner', 'lease_expires_at'])


def _rate_limit(account, owner, headers):
    with transaction.atomic():
        row, now = _locked(account), timezone.now()
        reservation = row.reservations.filter(request_id=owner).first()
        if not _owned(row, reservation, now):
            return _wait(row, now)
        candidates = [now + timedelta(seconds=1)]
        if row.blocked_until:
            candidates.append(row.blocked_until)
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
    account = account_key(client_id)
    post = post or requests.post

    def send(reservation, q, v, key):
        owner = reservation.request_id
        try:
            try:
                if not reservation.discovery:
                    business_requests_sent.set(business_requests_sent.get() + 1)
                response = post(URL, json={'query': q, 'variables': v},
                                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                                timeout=TIMEOUT, proxies=proxies)
            except requests.RequestException:
                return WclResult(error='network')
            if response.status_code == 429:
                return _rate_limit(account, owner, response.headers)
            if response.status_code != 200:
                return WclResult(error={401: 'authentication'}.get(
                    response.status_code, f'http_{response.status_code}'))
            try:
                payload = response.json()
            except (ValueError, TypeError):
                return WclResult(error='invalid_payload')
            if not isinstance(payload, dict):
                return WclResult(error='invalid_payload')
            deferred = _settle(account, owner, key, payload)
            if deferred:
                return deferred
            error = _error(payload)
            return WclResult(payload=payload if not error else None, error=error)
        finally:
            # Successful settlement removed the reservation. Errors retain its
            # charge without holding an active slot. Process death skips this;
            # the request fence expires, but its booked charge does not.
            _abandon(account, owner)

    try:
        reservation, measure, deferred = _reserve(account, uuid.uuid4().hex, cost_key, explicit_quota)
        if deferred:
            return deferred
        if reservation.discovery:
            discovery = send(reservation, QUOTA_QUERY, {}, '__quota__')
            if discovery.error:
                return discovery
            if not _quota(discovery.payload.get('data')):
                return WclResult(error='invalid_quota')
            # Recheck under the row lock: quota/429 or reset may change between
            # probe completion and business admission. Never retry discovery.
            reservation, measure, deferred = _reserve(account, uuid.uuid4().hex, cost_key, explicit_quota)
            if deferred:
                return deferred
            if reservation.discovery:
                _abandon(account, reservation.request_id)
                return WclResult(error='budget_wait', retry_at=timezone.now() + timedelta(seconds=1))
        result = send(reservation, measured_query if measure else query, variables, cost_key)
        if result.payload and not explicit_quota:
            data = dict(result.payload['data'])
            data.pop('_wclBudget', None)
            result = WclResult(payload={**result.payload, 'data': data})
        return result
    except OperationalError:
        # SQLite writer contention / backend lock timeout: defer, never spin.
        return WclResult(error='budget_wait', retry_at=timezone.now() + timedelta(seconds=1))
