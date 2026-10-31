"""Read-only, bounded actor projection over the canonical central text parts.

No actor cache, document join, long transaction or full-document decoder lives
here. JSON decoding is limited to one display action/header value; non-display
diagnostics retain only references and bounded raw fragments. The query oracle is
shared browser JavaScript, run as a streaming Node worker with backpressure.
"""
import hashlib
import json
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core import signing

from botend.models import SimcSkillDamageSnapshotActor
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Store
from botend.services.simc_skill_source_context import _fact, attach_skill_damage_source_context
from botend.services.simc_skill_descriptions import attach_skill_damage_descriptions

PRESENTATION_VERSION = 'skill-page-1'
PAGE_BYTES = 256 * 1024
RESPONSE_BYTES = 2 * 1024 * 1024
MAX_VALUE_CHARS = 2 * 1024 * 1024
MAX_HEADER_CHARS = 2 * 1024 * 1024
MAX_SOURCE_FACTS = 20000
MAX_SOURCE_CHARS = 2 * 1024 * 1024
# These are source diagnostics, not inputs to hero/global-card presentation.
# Keep unknown headers strict: deferring a display dependency would hide facts.
REFERENCE_HEADERS = frozenset({'specialization_passive_diagnostics'})
_SCAN_SLOTS = threading.BoundedSemaphore(1)
_SPECIAL = re.compile(r'["\\{}\[\]]')
_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class ActorPageError(ValueError):
    def __init__(self, message, *, code='page_unavailable', status=503, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class ActorReadControl:
    """Request-scoped cooperative cancellation; absent on the legacy path."""
    _local = threading.local()

    def __init__(self):
        self.cancel_event = threading.Event()
        self.admitted_event = threading.Event()
        self._lock = threading.Lock()
        self._interrupts = set()

    @classmethod
    def current(cls):
        return getattr(cls._local, 'control', None)

    @contextmanager
    def activate(self):
        previous = self.current()
        self._local.control = self
        try:
            yield self
        finally:
            self._local.control = previous

    def check(self):
        if self.cancel_event.is_set():
            raise ActorPageError('技能读取已取消', code='read_cancelled')

    def admit(self):
        self.check()
        self.admitted_event.set()

    def register(self, callback):
        with self._lock:
            self._interrupts.add(callback)
            if self.cancel_event.is_set():
                callback()

    def unregister(self, callback):
        with self._lock:
            self._interrupts.discard(callback)

    def cancel(self):
        with self._lock:
            self.cancel_event.set()
            for callback in self._interrupts:
                callback()


def _check_cancel():
    control = ActorReadControl.current()
    if control is not None:
        control.check()


def _admit_read():
    control = ActorReadControl.current()
    if control is not None:
        control.admit()


class _TextStream:
    def __init__(self, chunks):
        self.chunks, self.text, self.pos = iter(chunks), '', 0
        self.digest = hashlib.sha256()

    def ready(self):
        _check_cancel()
        while self.pos == len(self.text):
            try:
                self.text = next(self.chunks)
                _check_cancel()
            except StopIteration:
                return False
            if not isinstance(self.text, str):
                raise ValueError('Snapshot text chunk is not text')
            self.digest.update(self.text.encode('utf-8'))
            self.pos = 0
        return True

    def peek(self):
        return self.text[self.pos] if self.ready() else ''

    def whitespace(self):
        while self.ready():
            end = self.pos
            while end < len(self.text) and self.text[end] in ' \r\n\t':
                end += 1
            self.pos = end
            if self.pos < len(self.text):
                return

    def expect(self, token):
        self.whitespace()
        if self.peek() != token:
            raise ValueError('Malformed snapshot JSON structure')
        self.pos += 1

    def raw_value(self, *, cap=None, ordinal=None, fragment=None):
        """Lexically scan one value; retain only a capped value or raw slice.

        Search structural characters in C, not one Python operation per byte.
        Arrays/objects use a bounded nesting stack; quoted/escaped braces never
        terminate a value. The page path additionally JSON-decodes each value.
        """
        self.whitespace()
        start_char = self.peek()
        if not start_char or start_char in ',]}':
            raise ValueError('Missing snapshot JSON value')
        container = start_char in '[{'
        quoted = start_char == '"'
        stack, in_string, escaped = [], False, False
        fragments, length = [], 0

        def retain(piece):
            nonlocal length
            old = length
            length += len(piece)
            if cap is not None and length > cap:
                details = {}
                if ordinal is not None:
                    details['worker_error'] = {'code': 'action_too_large',
                        'source_ordinal': ordinal, 'origin': 'parser'}
                raise ActorPageError('单个技能事实或页头超过有界展示上限；可读取原始分段',
                    code='action_too_large' if ordinal is not None else 'header_too_large',
                    status=413, source_ordinal=ordinal, fragment_view='actor_fragment', **details)
            if fragment is not None:
                offset, size = fragment
                left, right = max(0, offset-old), min(len(piece), offset+size-old)
                if right > left:
                    fragments.append(piece[left:right])
            elif cap is not None:
                fragments.append(piece)

        while self.ready():
            begin = self.pos
            while self.pos < len(self.text):
                char = self.text[self.pos]
                if in_string:
                    if escaped:
                        escaped = False
                        self.pos += 1
                        continue
                    match = _SPECIAL.search(self.text, self.pos)
                    if not match:
                        self.pos = len(self.text)
                        break
                    self.pos = match.start()
                    char = self.text[self.pos]
                    self.pos += 1
                    if char == '\\':
                        escaped = True
                    elif char == '"':
                        in_string = False
                        if quoted and not container:
                            retain(self.text[begin:self.pos])
                            return ''.join(fragments), length
                    continue
                if char == '"':
                    in_string = True
                    self.pos += 1
                elif char in '[{':
                    stack.append(']' if char == '[' else '}')
                    if len(stack) > 128:
                        raise ValueError('Snapshot JSON nesting exceeds safety limit')
                    self.pos += 1
                elif char in ']}':
                    if not stack:
                        if container or quoted:
                            raise ValueError('Malformed snapshot JSON value')
                        retain(self.text[begin:self.pos])
                        return ''.join(fragments), length
                    if stack.pop() != char:
                        raise ValueError('Mismatched snapshot JSON brackets')
                    self.pos += 1
                    if not stack:
                        retain(self.text[begin:self.pos])
                        return ''.join(fragments), length
                elif not container and char in ', \r\n\t':
                    retain(self.text[begin:self.pos])
                    return ''.join(fragments), length
                else:
                    self.pos += 1
            retain(self.text[begin:self.pos])
        if stack or in_string or quoted or not length:
            raise ValueError('Truncated snapshot JSON value')
        return ''.join(fragments), length


class ActorDocumentParser:
    def __init__(self, chunks, *, max_value_chars=MAX_VALUE_CHARS, fragment=None,
                 header_fragment=None):
        self.stream = _TextStream(chunks)
        self.header = {}
        self.header_refs = []
        self.max_value_chars = max_value_chars
        self.fragment = fragment
        self.header_fragment = header_fragment
        self.fragment_result = None
        self.stored_actions = 0

    @property
    def document_sha256(self):
        return self.stream.digest.hexdigest()

    def actions(self):
        stream = self.stream
        stream.expect('{')
        keys, header_chars = set(), 0
        stream.whitespace()
        while stream.peek() != '}':
            raw_key, _ = stream.raw_value(cap=4096)
            key = json.loads(raw_key)
            if not isinstance(key, str) or key in keys:
                raise ValueError('Invalid or duplicate actor key')
            keys.add(key)
            stream.expect(':')
            if key == 'actions':
                stream.expect('[')
                stream.whitespace()
                while stream.peek() != ']':
                    ordinal = self.stored_actions
                    if self.fragment is None and self.header_fragment is None:
                        raw, _ = stream.raw_value(cap=self.max_value_chars, ordinal=ordinal)
                        action = json.loads(raw)
                        if not isinstance(action, dict):
                            raise ValueError('Snapshot action is not an object')
                        yield action
                    else:
                        target, offset, size = self.fragment or (None, 0, 0)
                        text, length = stream.raw_value(ordinal=ordinal,
                            fragment=(offset, size) if target == ordinal else (0, 0))
                        if target == ordinal:
                            self.fragment_result = {'text': text, 'total_characters': length,
                                'next_offset': min(offset + size, length) if offset + size < length else None}
                    self.stored_actions += 1
                    stream.whitespace()
                    if stream.peek() == ']':
                        break
                    stream.expect(',')
                    stream.whitespace()
                    if stream.peek() == ']':
                        raise ValueError('Trailing action comma')
                stream.expect(']')
            else:
                if self.header_fragment is not None:
                    target, offset, limit = self.header_fragment
                    text, size = stream.raw_value(
                        fragment=(offset, limit) if target == key else (0, 0))
                    if target == key:
                        self.fragment_result = {'text': text, 'total_characters': size,
                            'next_offset': offset + limit if offset + limit < size else None}
                elif self.fragment is not None:
                    # Action fragments must consume, not materialize unrelated headers.
                    stream.raw_value()
                elif key in REFERENCE_HEADERS:
                    _, size = stream.raw_value()
                    self.header_refs.append({'field': key, 'total_characters': size})
                else:
                    raw, size = stream.raw_value(cap=self.max_value_chars)
                    header_chars += size
                    if header_chars > MAX_HEADER_CHARS:
                        raise ActorPageError('技能页头超过有界展示上限', code='header_too_large', status=413)
                    self.header[key] = json.loads(raw)
            stream.whitespace()
            if stream.peek() == '}':
                break
            stream.expect(',')
            stream.whitespace()
            if stream.peek() == '}':
                raise ValueError('Trailing actor comma')
        stream.expect('}')
        stream.whitespace()
        # Forces the storage iterator through EOF SHA/length verification.
        if stream.peek():
            raise ValueError('Unexpected trailing snapshot data')
        if 'actions' not in keys:
            raise ValueError('Snapshot actor actions missing')


def compact_source_facts(actions):
    facts, length = {}, 0
    for row in actions:
        token = row.get('token')
        if not isinstance(token, str) or not token:
            continue
        fact = _fact(row)
        identity = (token, fact)
        if identity in facts:
            continue
        spell_id, parent, name = fact
        length += len(token) + len(parent) + len(name)
        if len(facts) >= MAX_SOURCE_FACTS or length > MAX_SOURCE_CHARS:
            raise ActorPageError('技能来源上下文超过有界展示上限', code='source_context_too_large', status=413)
        facts[identity] = {'token': token, 'spell_id': spell_id,
                           'parent_token': parent, 'display_name': name}
    return list(facts.values())


def attach_page_source_context(actor, facts):
    # Include all compact representatives in the existing resolver, including its
    # actor-wide label collision pass. Never resolve against only selected rows.
    attach_skill_damage_source_context({'actions': facts + actor['actions']})
    return actor


def normalize_query(params):
    def integer(key, default, low, high):
        try:
            value = int(params.get(key, default))
        except (ValueError, TypeError):
            raise ActorPageError(f'{key} 参数无效', code='invalid_query', status=400)
        if not low <= value <= high:
            raise ActorPageError(f'{key} 超过范围', code='invalid_query', status=400)
        return value
    try:
        exclude = json.loads(params.get('exclude', '[]'))
    except (ValueError, TypeError):
        raise ActorPageError('条件筛选参数无效', code='invalid_query', status=400)
    if (not isinstance(exclude, list) or len(exclude) > 500
            or any(not isinstance(key, str) or len(key) > 512 for key in exclude)):
        raise ActorPageError('条件筛选参数无效', code='invalid_query', status=400)
    sort, direction = params.get('sort', 'final'), params.get('direction', 'desc')
    if sort not in {'final', 'name'} or direction not in {'asc', 'desc'}:
        raise ActorPageError('排序参数无效', code='invalid_query', status=400)
    q = params.get('q', '')
    if not isinstance(q, str) or len(q) > 256:
        raise ActorPageError('搜索参数过长', code='invalid_query', status=400)
    return {'hero_tree_id': integer('hero_tree_id', 0, 0, 2147483647),
            'target_count': integer('target_count', 1, 1, 100), 'sort': sort,
            'direction': direction, 'q': q, 'exclude': sorted(set(exclude)),
            'limit': integer('limit', 50, 1, 100), 'byte_budget': PAGE_BYTES}


class _QueryPresentation:
    """Request-only compact display facts, resolved by the existing projector.

    Search/facet semantics see the same localized condition names and talent
    descriptions as the old full response, *before* the Node global query. Only
    the few fields the query reads are retained; no row or domain cache persists.
    """
    CONDITION_FIELDS = ('name_zh', 'display_name', 'description_zh', 'description')
    TALENT_FIELDS = ('talent_description_zh', 'talent_description')

    def __init__(self, row, snapshot):
        self.row, self.snapshot = row, snapshot
        self.cache, self.characters = {}, 0

    def _resolve(self, kind, source):
        fields = self.CONDITION_FIELDS if kind == 'condition' else self.TALENT_FIELDS
        identity_fields = ('spell_id',) if kind == 'condition' else ('talent_id', 'trait_entry_id')
        relevant = {key: source[key] for key in identity_fields + fields if key in source}
        key = (kind, _ENCODER.encode(relevant))
        if key not in self.cache:
            if len(self.cache) >= MAX_SOURCE_FACTS:
                raise ActorPageError('技能条件展示资料超过有界预算', code='condition_context_too_large', status=413)
            variant = {'runtime_conditions': [dict(source)]} if kind == 'condition' else dict(relevant)
            actor = {'class': self.row.get('class_name', ''),
                     'specialization': self.row.get('specialization', ''),
                     'actions': [{'variant': variant}]}
            attach_skill_damage_descriptions(actor, game_build=self.snapshot.game_build,
                branch=(self.snapshot.payload.get('identity') or {}).get('data_branch', 'retail'))
            projected = variant['runtime_conditions'][0] if kind == 'condition' else variant
            value = {field: projected[field] for field in fields if field in projected}
            self.characters += len(key[1]) + len(_ENCODER.encode(value))
            if self.characters > MAX_SOURCE_CHARS:
                raise ActorPageError('技能条件展示资料超过有界预算', code='condition_context_too_large', status=413)
            self.cache[key] = value
        source.update(self.cache[key])

    def apply(self, action):
        variant = action.get('variant')
        if not isinstance(variant, dict):
            return
        if variant.get('talent_id') or variant.get('trait_entry_id'):
            self._resolve('talent', variant)
        for condition in variant.get('runtime_conditions', []):
            if isinstance(condition, dict) and condition.get('spell_id'):
                self._resolve('condition', condition)


class NodeQueryRunner:
    """Full-duplex bounded IPC; concurrent readers avoid pipe/error deadlocks."""
    def __init__(self, query):
        script = Path(settings.BASE_DIR) / 'scripts/simc_skill_page_query.cjs'
        self.process = subprocess.Popen(['/usr/bin/node', '--max-old-space-size=128', str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.control = ActorReadControl.current()
        if self.control is not None:
            self.control.register(self.interrupt)
        self.output, self.errors, self.overflow = bytearray(), bytearray(), False
        self.timer = threading.Timer(300, self.process.kill)
        self.timer.daemon = True
        self.timer.start()
        self.threads = []
        for pipe, target, cap in ((self.process.stdout, self.output, RESPONSE_BYTES),
                                  (self.process.stderr, self.errors, 8192)):
            def read(pipe=pipe, target=target, cap=cap):
                while True:
                    data = pipe.read(65536)
                    if not data:
                        break
                    if len(target) + len(data) > cap:
                        self.overflow = True
                        self.process.kill()
                        break
                    target.extend(data)
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            self.threads.append(thread)
        try:
            self.send({'type': 'query', 'query': query})
        except BaseException:
            self.close()
            raise

    def send(self, frame):
        _check_cancel()
        # Exactly one bounded action, never the document. Batch JSON tokens to
        # avoid a syscall per field on the many-small-row production dataset.
        data = _ENCODER.encode(frame).encode('utf-8') + b'\n'
        view = memoryview(data)
        while view:
            _check_cancel()
            try:
                count = self.process.stdin.write(view[:65536])
            except BrokenPipeError:
                self.process.wait(timeout=5)
                for thread in self.threads:
                    thread.join(timeout=5)
                self._raise_worker_error(source_ordinal=frame.get('ordinal'))
            if not count:
                raise ActorPageError('技能查询进程不可用')
            view = view[count:]

    @staticmethod
    def _translate_worker_error(error):
        if not isinstance(error, dict):
            raise ActorPageError('技能查询进程返回无效错误', code='query_worker_failed')
        code = error.get('code', 'query_worker_failed')
        details = {'worker_error': error}
        if code == 'row_exceeds_page_budget':
            ordinal = error.get('source_ordinal')
            if type(ordinal) is not int or ordinal < 0:
                raise ActorPageError('技能查询进程返回无效行序号', code='query_worker_failed')
            details.update(source_ordinal=ordinal, fragment_view='actor_fragment')
            code = 'action_too_large'
        elif code == 'action_frame_exceeds_bounded_availability':
            code = 'action_too_large'
            ordinal = error.get('source_ordinal')
            if type(ordinal) is int and ordinal >= 0:
                details.update(source_ordinal=ordinal, fragment_view='actor_fragment')
        status = 413 if code in {'action_too_large', 'facet_budget_exceeded',
                                 'facets_exceed_page_budget'} else 503
        raise ActorPageError('技能事实或查询结果超过有界展示上限',
            code=code, status=status, **details)

    def _raise_worker_error(self, source_ordinal=None):
        message = self.errors.decode('utf-8', errors='replace').strip()
        match = re.fullmatch(r'row_exceeds_page_budget source_ordinal=([0-9]+)', message)
        if match:
            self._translate_worker_error({'code': 'row_exceeds_page_budget',
                'source_ordinal': int(match.group(1)), 'detail_view': 'actor_fragment'})
        if message in {'facet_budget_exceeded', 'facets_exceed_page_budget',
                       'action_frame_exceeds_bounded_availability'}:
            error = {'code': message}
            if source_ordinal is not None and message == 'action_frame_exceeds_bounded_availability':
                error['source_ordinal'] = source_ordinal
            self._translate_worker_error(error)
        raise ActorPageError('技能查询进程失败或超过资源上限', code='query_worker_failed')

    def finish(self):
        _check_cancel()
        self.process.stdin.close()
        code = self.process.wait(timeout=310)
        _check_cancel()
        for thread in self.threads:
            thread.join(timeout=5)
        if code or self.overflow:
            self._raise_worker_error()
        result = json.loads(self.output)
        if result.get('error'):
            self._translate_worker_error(result['error'])
        return result

    def interrupt(self):
        # Killing only the child unblocks pipe writes/wait in its owning thread.
        try:
            if self.process.poll() is None:
                self.process.kill()
        except ProcessLookupError:
            pass

    def close(self):
        if self.control is not None:
            self.control.unregister(self.interrupt)
        self.timer.cancel()
        self.interrupt()
        self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe and not pipe.closed:
                pipe.close()
        for thread in self.threads:
            thread.join(timeout=5)


class ActorPageService:
    def __init__(self, row, snapshot, *, runner_factory=NodeQueryRunner):
        self.row, self.snapshot, self.runner_factory = row, snapshot, runner_factory

    def _verify_membership(self, original_manifest, document='actor_payload'):
        fields = ['payload_storage']
        inline_actor = document not in Store._manifest(self.row)
        if inline_actor:
            fields.append(document)
        current = SimcSkillDamageSnapshotActor.objects.filter(pk=self.row['id'],
            snapshot_id=self.snapshot.pk, snapshot__status='succeeded',
            snapshot__simc_revision=self.snapshot.simc_revision,
            snapshot__game_build=self.snapshot.game_build,
            snapshot__schema_revision=self.snapshot.schema_revision).values(*fields).first()
        if (current is None or current['payload_storage'] != original_manifest
                or (inline_actor and current[document] != self.row[document])):
            raise ActorPageError('快照来源在读取期间发生变化', code='snapshot_changed')

    @staticmethod
    def _query_code_version():
        # Bind the actual shared comparator/worker and presentation projectors.
        root = Path(settings.BASE_DIR)
        digest = hashlib.sha256()
        for path in (root / 'static/dashboard/js/simc_skill_query.js',
                     root / 'scripts/simc_skill_page_query.cjs', Path(__file__),
                     Path(__file__).with_name('simc_skill_source_context.py'),
                     Path(__file__).with_name('simc_skill_descriptions.py')):
            with path.open('rb') as stream:
                digest.update(hashlib.file_digest(stream, 'sha256').digest())
        return digest.hexdigest()

    def page(self, query, cursor=''):
        if not _SCAN_SLOTS.acquire(blocking=False):
            raise ActorPageError('技能快照正在读取，请稍后重试', code='reader_busy')
        runner = None
        try:
            manifest = self.row['payload_storage']
            documents = Store._manifest(self.row)
            document = documents.get('actor_payload')
            document_sha256 = document['sha256'] if document else None
            if document is None:
                inline_digest = hashlib.sha256()
                for text in Store._iter_json_parts(self.row.get('actor_payload')):
                    inline_digest.update(text.encode('utf-8'))
                document_sha256 = inline_digest.hexdigest()
            binding = {'snapshot_id': self.snapshot.pk, 'actor_id': self.row['id'],
                       'document_sha256': document_sha256,
                       'query': query, 'presentation_version': PRESENTATION_VERSION,
                       'query_code_version': self._query_code_version()}
            after = None
            if cursor:
                if len(cursor) > 8192:
                    raise ActorPageError('分页游标无效', code='invalid_cursor', status=400)
                try:
                    decoded = signing.loads(cursor, salt=PRESENTATION_VERSION, max_age=86400)
                    if decoded['binding'] != binding:
                        raise ValueError('Cursor does not belong to this query')
                    after = decoded['after']
                except (signing.BadSignature, ValueError, KeyError, TypeError):
                    raise ActorPageError('分页游标与当前快照或筛选不匹配', code='invalid_cursor', status=400)
            _admit_read()
            parser = ActorDocumentParser(Store.iterate_json_document(self.row, 'actor_payload'))
            presentation = _QueryPresentation(self.row, self.snapshot)
            runner = self.runner_factory({**query, 'after': after})
            def feed():
                for ordinal, action in enumerate(parser.actions()):
                    _check_cancel()
                    presentation.apply(action)
                    runner.send({'type': 'action', 'ordinal': ordinal, 'action': action})
                    yield action
            facts = compact_source_facts(feed())
            result = runner.finish()
            if parser.document_sha256 != document_sha256:
                raise ValueError('Actor digest changed')
            _check_cancel()
            self._verify_membership(manifest)
            actions, ordinals = result['actions'], result['source_ordinals']
            if (not isinstance(actions, list) or len(actions) > query['limit']
                    or len(actions) != len(ordinals)):
                raise ValueError('Invalid worker page')
            actor = {**parser.header, 'actions': actions}
            attach_page_source_context(actor, facts)
            page = result['page']
            next_cursor = signing.dumps({'binding': binding, 'after': page['after']},
                salt=PRESENTATION_VERSION, compress=True) if page['has_more'] else None
            return actor, {'document_sha256': parser.document_sha256,
                'presentation_version': PRESENTATION_VERSION,
                'query_code_version': binding['query_code_version'],
                'header_refs': [{**ref, 'view': 'actor_header_fragment',
                    'document': 'actor_payload', 'document_sha256': parser.document_sha256,
                    'snapshot_id': self.snapshot.pk, 'actor_id': self.row['id'],
                    'offset_unit': 'unicode_characters', 'fragment_limit': 16384}
                    for ref in parser.header_refs],
                'counts': {**result['counts'], 'stored_actions': parser.stored_actions},
                'facets': result['facets'], 'source_ordinals': ordinals,
                'page': {'returned': len(actions), 'has_more': bool(page['has_more']),
                         'next_cursor': next_cursor},
                'unresolved': {'availability': 'separate_document',
                    'document': 'unresolved_payload', 'actor_id': self.row['id'],
                    'snapshot_id': self.snapshot.pk, 'view': 'unresolved_fragment'}}
        except ActorPageError:
            raise
        except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as exc:
            raise ActorPageError('技能快照数据或查询进程暂时不可用') from exc
        finally:
            try:
                if runner is not None:
                    runner.close()
            finally:
                _SCAN_SLOTS.release()

    def fragment(self, *, ordinal, offset, limit, document='actor_payload', field=None):
        if not _SCAN_SLOTS.acquire(blocking=False):
            raise ActorPageError('技能快照正在读取，请稍后重试', code='reader_busy')
        try:
            manifest = self.row['payload_storage']
            Store._manifest(self.row)
            _admit_read()
            chunks = Store.iterate_json_document(self.row, document)
            if document == 'actor_payload':
                parser = ActorDocumentParser(chunks,
                    fragment=(ordinal, offset, limit) if field is None else None,
                    header_fragment=(field, offset, limit) if field is not None else None)
                list(parser.actions())  # yields no full actions in fragment mode
                result = parser.fragment_result
                digest = parser.document_sha256
                if result is None:
                    if field is not None:
                        raise ActorPageError('技能原始页头字段不存在', status=404, code='header_not_found')
                    raise ActorPageError('技能原始序号不存在', status=404, code='action_not_found')
            else:
                digest_obj, pieces, length = hashlib.sha256(), [], 0
                for text in chunks:
                    _check_cancel()
                    digest_obj.update(text.encode('utf-8'))
                    left, right = max(0, offset-length), min(len(text), offset+limit-length)
                    if right > left:
                        pieces.append(text[left:right])
                    length += len(text)
                digest = digest_obj.hexdigest()
                result = {'text': ''.join(pieces), 'total_characters': length,
                    'next_offset': min(offset+limit, length) if offset+limit < length else None}
            _check_cancel()
            self._verify_membership(manifest, document)
            if field is not None:
                result['field'] = field
            return {**result, 'document_sha256': digest, 'document': document,
                    'source_ordinal': ordinal if document == 'actor_payload' and field is None else None,
                    'offset': offset, 'offset_unit': 'unicode_characters',
                    'snapshot_id': self.snapshot.pk, 'actor_id': self.row['id']}
        except ActorPageError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise ActorPageError('技能快照原始片段暂时不可用') from exc
        finally:
            _SCAN_SLOTS.release()
