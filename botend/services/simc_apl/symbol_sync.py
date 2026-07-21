"""Build and transactionally synchronize auditable SimC APL symbol facts."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from django.conf import settings
from django.db import transaction

from botend.models import SimcApl, SimcAplSymbol
from .ast import ActionAssignment, ActionEntry, Option
from .expression import is_valid_identifier
from .validation import validate_document


# Task 6 has no runtime introspection manifest yet (that is deliberately Task 7).
# These are the small, engine-owned symbols whose spelling is stable and audited;
# everything class/spec specific still has to be observed in the parser AST or
# supplied as an explicit token -> SpellID binding.
ENGINE_PSEUDO_ACTIONS = frozenset({
    'call_action_list', 'cycling_variable', 'pool_resource', 'run_action_list',
    'snapshot_stats', 'variable', 'wait',
})
ENGINE_OPTIONS = frozenset({
    'cancel_if', 'early_chain_if', 'if', 'interrupt_if', 'target_if',
})
ENGINE_NAMESPACES = frozenset({
    'action', 'buff', 'cooldown', 'debuff', 'dot', 'hero_tree', 'talent',
    'variable',
})


@dataclass(frozen=True)
class BuildResult:
    facts: Tuple[dict, ...]
    completeness: str = 'observed/partial'
    unbound: int = 0
    invalid: int = 0


@dataclass(frozen=True)
class SyncSummary:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    unbound: int = 0
    invalid: int = 0
    completeness: str = 'observed/partial'


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_COMPLETENESS = frozenset({'partial', 'complete'})
MANIFEST_SCOPES = frozenset({'global', 'class', 'spec', 'hero_tree'})


def _manifest_error(message):
    raise ValueError(f'invalid runtime APL manifest: {message}')


def _string_or_none(value, field, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        _manifest_error(f'{field} must be a non-empty string')
    return value.strip().lower() or None


def load_runtime_manifest(path, simc_revision, wow_build):
    """Strictly load revision/build-bound facts exported by the patched binary."""
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'invalid runtime APL manifest JSON: {exc}') from exc
    if not isinstance(payload, dict):
        _manifest_error('root must be an object')
    if payload.get('schema_version') != MANIFEST_SCHEMA_VERSION:
        _manifest_error(f'unsupported schema_version {payload.get("schema_version")!r}')
    if (not isinstance(simc_revision, str) or
            not re.fullmatch(r'[0-9a-fA-F]{40}', simc_revision) or
            payload.get('simc_revision') != simc_revision):
        _manifest_error('simc revision mismatch or must be a 40-hex SHA')
    if payload.get('game_build') != wow_build:
        _manifest_error('game_build mismatch')

    completeness = payload.get('completeness')
    if not isinstance(completeness, dict):
        _manifest_error('completeness must be an object')
    status = completeness.get('status')
    modules = completeness.get('modules')
    limitations = completeness.get('limitations')
    if (status not in MANIFEST_COMPLETENESS or not isinstance(modules, dict) or
            not all(isinstance(key, str) and isinstance(value, str)
                    for key, value in modules.items()) or
            not isinstance(limitations, list) or
            not all(isinstance(value, str) for value in limitations)):
        _manifest_error('malformed completeness declaration')
    required_modules = {'global_options', 'actions', 'action_options', 'expressions', 'class_specs'}
    if not required_modules.issubset(modules):
        _manifest_error('completeness modules must declare global_options/actions/action_options/expressions/class_specs')
    if status == 'complete' and (limitations or any(
            value.lower() not in {'complete', 'runtime_initialized'}
            for value in modules.values())):
        _manifest_error('completeness cannot be complete with limitations or failed modules')

    symbols = payload.get('symbols')
    if not isinstance(symbols, list):
        _manifest_error('symbols must be an array')
    valid_kinds = {choice[0] for choice in SimcAplSymbol.SYMBOL_KIND_CHOICES}
    facts = {}
    for index, symbol in enumerate(symbols):
        prefix = f'symbols[{index}]'
        if not isinstance(symbol, dict):
            _manifest_error(f'{prefix} must be an object')
        required = {'class', 'spec', 'scope', 'token', 'kind', 'spell_id',
                    'source', 'options', 'aliases'}
        if not required.issubset(symbol):
            _manifest_error(f'{prefix} is missing required fields')
        scope = symbol.get('scope')
        kind = symbol.get('kind')
        spell_id = symbol.get('spell_id')
        options = symbol.get('options')
        aliases = symbol.get('aliases')
        if (scope not in MANIFEST_SCOPES or kind not in valid_kinds or
                (spell_id is not None and (not isinstance(spell_id, int) or
                                           isinstance(spell_id, bool) or spell_id <= 0)) or
                not isinstance(options, list) or
                not all(isinstance(value, str) and value.strip() for value in options) or
                not isinstance(aliases, list) or
                not all(isinstance(value, str) and value.strip() for value in aliases) or
                not isinstance(symbol.get('source'), str) or not symbol['source'].strip()):
            _manifest_error(f'{prefix} has invalid field types or values')
        class_name = _string_or_none(symbol.get('class'), f'{prefix}.class')
        spec = _string_or_none(symbol.get('spec'), f'{prefix}.spec')
        token = _string_or_none(symbol.get('token'), f'{prefix}.token', required=True)
        if ((scope == 'global' and (class_name or spec)) or
                (scope == 'class' and (not class_name or spec)) or
                (scope in {'spec', 'hero_tree'} and (not class_name or not spec))):
            _manifest_error(f'{prefix} scope does not match class/spec')
        if token in {'apl_metadata_export', 'apl_metadata_revision', 'apl_metadata_game_build'}:
            _manifest_error(f'{prefix} contains a control option')
        fact = {
            'class_name': class_name, 'spec': spec, 'hero_tree': None,
            'token': token, 'symbol_kind': kind, 'spell_id': spell_id,
            'source': SimcAplSymbol.SOURCE_SIMC_MANIFEST,
            'options': sorted(set(value.strip().lower() for value in options)),
            'aliases': sorted(set(value.strip().lower() for value in aliases)),
        }
        identity = _identity(fact)
        if identity in facts and facts[identity] != fact:
            _manifest_error(f'{prefix} conflicts with a duplicate identity')
        facts[identity] = fact
    ordered = tuple(facts[key] for key in sorted(facts))
    return BuildResult(ordered, completeness=f'runtime/{status}', unbound=sum(
        fact['symbol_kind'] == SimcAplSymbol.KIND_ACTION and fact.get('spell_id') is None
        for fact in ordered))


def _canonical_scope(apl):
    class_name = (apl.class_name or '').strip().lower()
    spec_key = (apl.spec or '').strip().lower()
    prefix = f'{class_name}_'
    spec = spec_key[len(prefix):] if class_name and spec_key.startswith(prefix) else spec_key
    return class_name or None, spec or None


def _identity(fact):
    return (
        fact.get('class_name') or '', fact.get('spec') or '', fact.get('hero_tree') or '',
        str(fact.get('token') or '').strip().lower(), fact.get('symbol_kind', 'action'),
    )


def build_symbol_facts(simc_revision, wow_build, apl_queryset=None, bindings=None,
                       manifest_path=None):
    """Scan parser/AST output only; this is observed corpus coverage, not complete SimC."""
    apl_queryset = apl_queryset if apl_queryset is not None else SimcApl.objects.filter(
        source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True, is_active=True,
        sync_version=simc_revision,
    )
    facts = {}
    invalid = 0
    for token in ENGINE_PSEUDO_ACTIONS:
        fact = {'class_name': None, 'spec': None, 'token': token,
                'symbol_kind': SimcAplSymbol.KIND_PSEUDO_ACTION,
                'source': SimcAplSymbol.SOURCE_MANUAL}
        facts[_identity(fact)] = fact
    for token in ENGINE_OPTIONS:
        fact = {'class_name': None, 'spec': None, 'token': token,
                'symbol_kind': SimcAplSymbol.KIND_OPTION,
                'source': SimcAplSymbol.SOURCE_MANUAL}
        facts[_identity(fact)] = fact
    for token in ENGINE_NAMESPACES:
        fact = {'class_name': None, 'spec': None, 'token': token,
                'symbol_kind': SimcAplSymbol.KIND_NAMESPACE,
                'source': SimcAplSymbol.SOURCE_MANUAL}
        facts[_identity(fact)] = fact
    for apl in apl_queryset:
        class_name, spec = _canonical_scope(apl)
        document, result, errors = validate_document(apl.content)
        invalid += len(errors)
        if errors:
            continue
        for token, _, _ in result.symbols.actions:
            pseudo = token in ENGINE_PSEUDO_ACTIONS
            fact = {'class_name': None if pseudo else class_name,
                    'spec': None if pseudo else spec, 'token': token,
                    'symbol_kind': (SimcAplSymbol.KIND_PSEUDO_ACTION
                                    if pseudo else SimcAplSymbol.KIND_ACTION),
                    'source': (SimcAplSymbol.SOURCE_MANUAL if pseudo
                               else SimcAplSymbol.SOURCE_SYSTEM_APL)}
            facts[_identity(fact)] = fact
        for line in document.lines:
            if not isinstance(line, ActionAssignment):
                continue
            for action in line.actions:
                if not isinstance(action, ActionEntry):
                    continue
                for option in action.options:
                    if not isinstance(option, Option):
                        continue
                    fact = {'class_name': class_name, 'spec': spec,
                            'token': option.name.strip().lower(),
                            'symbol_kind': SimcAplSymbol.KIND_ACTION_OPTION,
                            'source': SimcAplSymbol.SOURCE_SYSTEM_APL}
                    facts[_identity(fact)] = fact
        for expression, _, _ in result.symbols.expression_identifiers:
            expression = expression.strip().lower()
            namespace = expression.split('.', 1)[0]
            if ('.' not in expression or not namespace or
                    not is_valid_identifier(expression)):
                invalid += 1
                continue
            expression_fact = {
                'class_name': class_name, 'spec': spec, 'token': expression,
                'symbol_kind': SimcAplSymbol.KIND_EXPRESSION,
                'source': SimcAplSymbol.SOURCE_SYSTEM_APL,
            }
            facts[_identity(expression_fact)] = expression_fact
            fact = {'class_name': class_name, 'spec': spec, 'token': namespace,
                    'symbol_kind': SimcAplSymbol.KIND_NAMESPACE,
                    'source': SimcAplSymbol.SOURCE_SYSTEM_APL}
            facts[_identity(fact)] = fact

    explicit = bindings if bindings is not None else getattr(settings, 'SIMC_APL_SYMBOL_BINDINGS', [])
    for binding in explicit or []:
        if not isinstance(binding, dict):
            invalid += 1
            continue
        required = {'token', 'symbol_kind', 'spell_id', 'class_name', 'spec', 'hero_tree'}
        if not required.issubset(binding):
            invalid += 1
            continue
        token = str(binding.get('token') or '').strip().lower()
        kind = str(binding.get('symbol_kind') or '').strip()
        spell_id = binding.get('spell_id')
        valid_kinds = {choice[0] for choice in SimcAplSymbol.SYMBOL_KIND_CHOICES}
        if (not token or kind not in valid_kinds or not isinstance(spell_id, int) or
                isinstance(spell_id, bool)):
            invalid += 1
            continue
        scope = {
            'class_name': str(binding.get('class_name') or '').strip().lower() or None,
            'spec': str(binding.get('spec') or '').strip().lower() or None,
            'hero_tree': str(binding.get('hero_tree') or '').strip().lower() or None,
        }
        matching = [key for key, fact in facts.items()
                    if fact['token'] == token and fact['symbol_kind'] == kind and
                    all(fact.get(field) == scope[field]
                        for field in ('class_name', 'spec', 'hero_tree'))]
        if len(matching) != 1:
            invalid += 1
            continue
        key = matching[0]
        facts[key] = dict(facts[key], spell_id=spell_id,
                          source=SimcAplSymbol.SOURCE_MANUAL)

    completeness = 'observed/partial'
    if manifest_path:
        runtime = load_runtime_manifest(manifest_path, simc_revision, wow_build)
        # Runtime-created payload is authoritative at an exact identity. Facts
        # only observed in official APLs remain available with their weaker source.
        for fact in runtime.facts:
            facts[_identity(fact)] = fact
        completeness = runtime.completeness
    ordered = tuple(facts[key] for key in sorted(facts))
    return BuildResult(ordered, completeness=completeness, unbound=sum(
        1 for fact in ordered if fact['symbol_kind'] == SimcAplSymbol.KIND_ACTION and
        fact.get('spell_id') is None
    ), invalid=invalid)


def _snapshot(revision, build):
    fields = ('class_key', 'spec_key', 'hero_tree_key', 'token', 'symbol_kind',
              'class_name', 'spec', 'hero_tree', 'spell_id', 'source', 'aliases',
              'options', 'is_active')
    return {tuple(row[:5]): tuple(row[5:]) for row in
            SimcAplSymbol.objects.filter(simc_revision=revision, wow_build=build)
            .values_list(*fields)}


def sync_symbols(simc_revision, wow_build, *, dry_run=False, apl_queryset=None,
                 bindings=None, manifest_path=None):
    """Build/validate completely before the atomic upsert/deactivation boundary."""
    if not str(simc_revision or '').strip() or not str(wow_build or '').strip():
        raise ValueError('simc_revision and wow_build are required')
    result = build_symbol_facts(simc_revision, wow_build, apl_queryset, bindings,
                                manifest_path=manifest_path)
    if result.invalid:
        if dry_run:
            return SyncSummary(unbound=result.unbound, invalid=result.invalid,
                               completeness=result.completeness)
        raise ValueError(f'{result.invalid} invalid APL diagnostic(s) or symbol binding(s)')
    before = _snapshot(simc_revision, wow_build)
    if dry_run:
        # Use rollback to ensure the preview exercises real model validation/diff.
        with transaction.atomic():
            SimcAplSymbol.sync_revision_catalog(simc_revision, wow_build, result.facts)
            after = _snapshot(simc_revision, wow_build)
            transaction.set_rollback(True)
    else:
        SimcAplSymbol.sync_revision_catalog(simc_revision, wow_build, result.facts)
        after = _snapshot(simc_revision, wow_build)
    created = sum(key not in before and payload[-1] for key, payload in after.items())
    updated = sum(key in before and payload[-1] and before[key] != payload
                  for key, payload in after.items())
    unchanged = sum(key in before and payload[-1] and before[key] == payload
                    for key, payload in after.items())
    deactivated = sum(payload[-1] and key in after and not after[key][-1]
                      for key, payload in before.items())
    return SyncSummary(created, updated, unchanged, deactivated,
                       result.unbound, result.invalid, result.completeness)
