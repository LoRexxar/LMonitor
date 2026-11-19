"""根据已发布技能快照的各目标伤害推导个人 APL。"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import groupby
from math import isfinite
import re

from botend.constants.hero_talents import hero_subtree_name_by_id
from .ast import ActionAssignment, ActionEntry, Option
from .hero_filter import hero_slug
from .parser import parse
from .validation import validate_document


TARGETS = (1, 2, 5, 10, 20)
STATE = re.compile(r'(buff|debuff)\.[a-z0-9_]+')
COMMON = {'precombat', 'trinkets', 'items', 'variables', 'cooldowns', 'cds'}


class SkillAplGenerationError(ValueError):
    """当前专精的快照或模板无法生成有效的优先级。"""


def _alternatives(guards):
    guards = list(dict.fromkeys(guards))
    if not guards or '' in guards:
        return ''
    return guards[0] if len(guards) == 1 else '(' + '|'.join(guards) + ')'


def _and(*guards):
    return '&'.join(g for g in guards if g)


def _command(token, guard):
    return token + (',if=' + guard if guard else '')


def _stack_guard(state, levels):
    if levels is None:
        return state + '.up' if STATE.fullmatch(state) else state
    levels = sorted(levels)
    if len(levels) >= 3 and levels[-1] - levels[0] + 1 == len(levels):
        return f'{state}.stack>={levels[0]}&{state}.stack<={levels[-1]}'
    return _alternatives(f'{state}.stack={level}' for level in levels)


def _combine_states(terms):
    """每项是一组同时成立的状态，多项取或。空项代表无条件施放。"""
    if any(not term for term in terms):
        return ""
    # 宽条件包含窄条件，例如 up 包含 stack=2。
    minimal = []
    for term in terms:
        if term in minimal:
            continue
        if any(other != term and all(
            state in term and (levels is None or term[state] is not None and term[state] <= levels)
            for state, levels in other.items()
        ) for other in terms):
            continue
        minimal.append(term)

    counts = Counter(state for term in minimal for state in term)
    state = min(counts, key=lambda key: (-counts[key], key))
    alternatives = []
    independent = [term for term in minimal if state not in term]
    if independent:
        alternatives.append(_combine_states(independent))
    by_levels = {}
    for term in minimal:
        if state in term:
            rest = {key: value for key, value in term.items() if key != state}
            by_levels.setdefault(term[state], []).append(rest)
    # 其余前提一致时把同一状态的层数合并，保留不连续的层数和上界。
    by_rest = {}
    for levels, rest in by_levels.items():
        by_rest.setdefault(_combine_states(rest), []).append(levels)
    for guard, choices in by_rest.items():
        levels = None if None in choices else frozenset().union(*choices)
        head = _stack_guard(state, levels)
        alternatives.append(head + "&" + guard if guard else head)
    return alternatives[0] if len(alternatives) == 1 else "(" + "|".join(alternatives) + ")"



def _talent_guard(name):
    if not name or any(c in name for c in '+.%'):
        return ''
    token = re.sub(r"[^a-z0-9_]", "", name.lstrip('_').lower().replace(' ', '_'))
    return 'talent.' + token if token else ''


def native_talent_guards(catalog, class_id, spec_id):
    """按 SimC 的树查找顺序及原生条目顺序区分同名天赋。"""
    trees = {2: defaultdict(list), 3: defaultdict(list), 1: defaultdict(list)}
    guards = {}
    for row in catalog:
        if row['class_id'] != class_id:
            continue
        tree = row['tree_index']
        allowed = not row['spec_ids'] or spec_id in row['spec_ids']
        if tree == 4 and allowed:
            name = hero_subtree_name_by_id(row['subtree_id'])
            if name:
                guards[row['trait_entry_id']] = 'hero_tree.' + hero_slug(name)
        # SimC 查找英雄天赋时会遍历职业的全部英雄树，同名编号也包含其他专精。
        if tree not in trees or tree != 3 and not allowed:
            continue
        guard = _talent_guard(row.get('name'))
        if guard and not re.search(r'_\d+$', guard):
            trees[tree][guard].append((row['trait_entry_id'], allowed))
    claimed = set()
    for by_name in trees.values():
        for guard, entries in by_name.items():
            if guard in claimed:
                continue
            claimed.add(guard)
            for index, (entry, allowed) in enumerate(entries, start=1):
                if allowed:
                    guards[entry] = guard if len(entries) == 1 else f'{guard}_{index}'
    return guards


def _conditions(variant, catalog, template_talents):
    states = {}
    traits_needed = []
    if variant.get('talent_id') or variant.get('trait_entry_id'):
        traits_needed.append((variant.get('trait_entry_id'), variant.get('talent_name')))
    for field in ('activation_context', 'reference_context'):
        context = variant.get(field) or {}
        traits = context.get('traits') or []
        if set(context.get('trait_entry_ids') or []) - {t.get('trait_entry_id') for t in traits}:
            return None
        traits_needed.extend((t.get('trait_entry_id'), t.get('name')) for t in traits)
    for entry, name in traits_needed:
        guard = catalog.get(entry) or _talent_guard(name)
        if entry not in catalog and guard not in template_talents:
            return None
        states[guard] = None

    # 本地化器追加的状态说明来自下方的结构化条件。血量来自高低血量配对。
    condition = (variant.get('runtime_condition') or '').split('，且')[0]
    for part in filter(None, condition.split('，')):
        if part == '血量低于35%':
            states['target.health.pct<35'] = None
        elif part.startswith(('自身存在', '目标存在')) and variant.get('runtime_conditions'):
            continue
        elif not (part.startswith('点出') and traits_needed):
            return None

    conditions = [*(variant.get('runtime_conditions') or []), *(variant.get('activation_conditions') or [])]
    if set(variant.get('scenario_tokens') or []) - {c.get('token') for c in conditions}:
        return None
    for condition in conditions:
        state = condition.get('token') or ''
        if not STATE.fullmatch(state):
            return None
        stacks = condition.get('stacks', 1)
        levels = condition.get('stack_values') or ([stacks] if stacks != 1 else [])
        if any(type(v) is not int or v < 1 for v in levels):
            return None
        if levels:
            allowed = frozenset(levels)
            if states.get(state) is not None:
                allowed &= states[state]
            if not allowed:
                return None
            states[state] = allowed
        else:
            states.setdefault(state, None)
    return states


def _samples(rows, tokens, catalog, template_talents):
    samples = defaultdict(list)
    for row in rows:
        token = row.get('token')
        if token not in tokens:
            continue
        # 猫形态横扫等动作由默认 APL 直接施放，但原生以派生行报告。
        # 对此类行要求名称与法术 ID 同时匹配自身报告根，排除其他技能的附伤。
        if row.get('player_skill') is False and (
                row.get('reporting_root_token') != token or not row.get('spell_id')
                or row.get('reporting_root_spell_id') != row['spell_id']):
            continue
        variant = row.get('variant') or {}
        states = _conditions(variant, catalog, template_talents)
        if states is None:
            continue
        owners = set(row.get('hero_subtree_ids') or [])
        tree = variant.get('hero_subtree_id')
        if tree:
            if owners and tree not in owners:
                continue
            owners = {tree}
        amounts = (row.get('product') or {}).get('final_normalized_damage_by_target') or {}
        affected = row.get('affected_target_counts', TARGETS)
        damage = {n: amounts[str(n)] for n in TARGETS if n in affected and
                  type(amounts.get(str(n))) in (int, float) and isfinite(amounts[str(n)]) and amounts[str(n)] > 0}
        if damage:
            samples[token].append((owners, damage, states))
    return samples


def _ranked(samples, entries, hero, count):
    # 每个技能保留默认列表给出的可施放条件。未命中测量状态时由原优先级兜底。
    eligible = defaultdict(list)
    for token, guard in entries:
        eligible[token].append(guard)
    candidates = []
    for token, guards in eligible.items():
        guard = _alternatives(guards)
        for owners, damage, state in samples.get(token.partition(',')[0], ()):
            if count not in damage or owners and hero not in owners:
                continue
            term = {**state, guard: None} if guard else state
            keys = frozenset(term.items())
            candidates.append((-damage[count], len(keys), token, tuple(sorted(_stack_guard(k, v) for k, v in keys)), keys, term))
    candidates.sort(key=lambda item: item[:4])
    seen = defaultdict(list)
    ordered = []
    for _, _, token, _, keys, term in candidates:
        if any(previous <= keys for previous in seen[token]):
            continue
        seen[token].append(keys)
        ordered.append((token, term))
    return [(token, _combine_states([term for _, term in run])) for token, run in groupby(ordered, key=lambda r: r[0])]


def _merged(entries):
    seen = set()
    result = []
    for token, run in groupby(entries, key=lambda r: r[0]):
        guard = _alternatives(g for _, g in run)
        if token not in seen:
            result.append((token, guard))
        if not guard:
            seen.add(token)
    return result


def _tier_guard(first, last):
    index = TARGETS.index(last)
    lower = f'active_enemies>={first}' if first > 1 else ''
    upper = f'active_enemies<{TARGETS[index + 1]}' if index + 1 < len(TARGETS) else ''
    return _and(lower, upper)


def _target_guard(counts):
    windows = []
    for selected, run in groupby(TARGETS, key=lambda n: n in counts):
        tiers = list(run)
        if selected:
            windows.append(_tier_guard(tiers[0], tiers[-1]))
    return _alternatives(windows)


def _case_guard(cases, heroes):
    trees_by_range = defaultdict(list)
    for hero, slug in heroes:
        if hero in cases:
            trees_by_range[_target_guard(cases[hero])].append('hero_tree.' + slug)
    return _alternatives(_and(_alternatives(trees), window) for window, trees in trees_by_range.items())


def _generic_block(entries, samples, heroes):
    # 对齐各分支中相同的施法行。每个分支单独投影后仍保持原先的降序。
    baseline = _merged(entries)
    cases_by_sequence = defaultdict(list)
    compared = False
    for hero, _ in heroes:
        for count in TARGETS:
            ranked = _ranked(samples, entries, hero, count)
            compared |= len({token.partition(',')[0] for token, _ in ranked}) >= 2
            if ranked and _merged([*ranked, *baseline]) != baseline:
                cases_by_sequence[tuple(ranked)].append((hero, count))

    aligned = []
    for sequence, cases in cases_by_sequence.items():
        previous = [command for command, _ in aligned]
        updated = []
        for op, i, j, start, stop in SequenceMatcher(None, previous, sequence, autojunk=False).get_opcodes():
            if op == 'equal':
                updated.extend((cmd, owners + cases) for cmd, owners in aligned[i:j])
            else:
                updated.extend(aligned[i:j])
                updated.extend((cmd, cases) for cmd in sequence[start:stop])
        aligned = updated
    result = []
    for (token, guard), cases in aligned:
        counts_by_tree = defaultdict(set)
        for hero, count in cases:
            counts_by_tree[hero].add(count)
        result.append((token, _and(_case_guard(counts_by_tree, heroes), guard)))
    return _merged([*result, *baseline]), compared


def _fury_branch(entries, samples, hero, slug, count):
    tokens = list(dict.fromkeys(token for token, _ in entries))
    prefix = [(t, '') for t in ('recklessness', 'avatar') if t in tokens]
    active = [t for t in tokens if t not in ('recklessness', 'avatar', 'storm_bolt')]
    tail = []
    if count > 1:
        maintain = 'talent.improved_whirlwind&buff.whirlwind.down'
        if slug == 'mountain_thane' and 'thunder_clap' in active:
            prefix.append(('thunder_clap', _and('talent.crashing_thunder', maintain)))
            active.remove('thunder_clap')
            tail.append(('thunder_clap', ''))
            maintain = _and('!talent.crashing_thunder', maintain)
        if 'whirlwind' in active:
            prefix.append(('whirlwind', maintain))
            active.remove('whirlwind')
            tail.append(('whirlwind', ''))
    if 'thunder_blast' in active:
        guard = 'buff.thunder_blast.stack=2|buff.thunder_blast.remains<gcd|buff.avatar.up'
        if count > 1:
            guard += '|talent.crashing_thunder&talent.improved_whirlwind&buff.whirlwind.down'
        prefix.append(('thunder_blast', guard))
    if 'rampage' in active:
        prefix.append(('rampage', 'buff.enrage.remains<gcd|rage>=100'))
    ranked = _ranked(samples, [(t, '') for t in active], hero, count)
    fallback = [(t, g) for t, g in entries if t in active]
    if 'storm_bolt' in tokens:
        tail.append(('storm_bolt', 'buff.bladestorm.up'))
    measured = {token for token, _ in ranked}
    if measured == set(active):
        return _merged(prefix + ranked + fallback + tail), len(measured) >= 2

    # 单项天赋快照可能缺少某个施法技能。保留它在默认循环的位置及前后条件，
    # 避免把它放在始终可用的填充技能后，导致该技能整场都无法施放。
    rotation = []
    compared = False
    for has_damage, run in groupby(fallback, key=lambda item: item[0] in measured):
        segment = list(run)
        if has_damage:
            rotation.extend(_ranked(samples, segment, hero, count))
            compared |= len({token for token, _ in segment}) >= 2
        rotation.extend(segment)
    return _merged(prefix + rotation + tail), compared


# 在默认循环已经处理完攒能、团队增益和爆发之后，仅比较剩余填充选择。
# 狂徒只消耗已触发的手枪，增辉只在精华已满时额外比较爆发。
FILLER_CHOICES = {
    'rogue_outlaw': ('build', 'sinister_strike', 'pistol_shot', 'buff.opportunity.up'),
    'evoker_augmentation': ('filler', 'azure_strike', 'eruption', 'essence.deficit=0'),
}


@dataclass(frozen=True)
class _Entry:
    group: str | None
    operator: str
    token: str
    command: str
    guard: str


def _entry(line):
    if not isinstance(line, ActionAssignment) or len(line.actions) != 1:
        return None
    action = line.actions[0]
    if not isinstance(action, ActionEntry) or not all(isinstance(o, Option) for o in action.options):
        return None
    options = []
    guard = ''
    for option in action.options:
        if option.name == 'if':
            guard = '(' + option.value + ')' if '|' in option.value else option.value
        elif option.name in {'target_if', 'empower_to'}:
            options.append(f'{option.name}={option.value}')
        else:
            return None
    return _Entry(line.list_name, line.operator, action.name, ','.join([action.name, *options]), guard)


def build_apl(template, rows, trees, spec, *, talent_guards=None):
    """消费一次技能行迭代器，仅保留与默认循环动作匹配的条件及五档伤害。"""
    lines = template.splitlines()
    document = parse(template)
    matches = [_entry(line) for line in document.lines]
    # for_next 以原列表的下一项作为资源目标，必须保留该动作的位置和判断条件。
    pool_next = set()
    for index, line in enumerate(document.lines):
        if not isinstance(line, ActionAssignment):
            continue
        if line.list_name in pool_next:
            matches[index] = None
            pool_next.remove(line.list_name)
        if any(isinstance(action, ActionEntry) and action.name == 'pool_resource'
               and any(isinstance(opt, Option) and opt.name == 'for_next' and opt.value != '0'
                       for opt in action.options) for action in line.actions):
            pool_next.add(line.list_name)
    template_talents = set(re.findall(r'\btalent\.[a-z0-9_]+', '\n'.join(
        line for line in lines if line.lstrip().startswith('actions'))))
    samples = _samples(rows, {m.token for m in matches if m and m.group not in COMMON},
                       talent_guards or {}, template_talents)
    heroes = [(tree['id'], hero_slug(hero_subtree_name_by_id(tree['id']))) for tree in trees]
    if not heroes or any(not slug for _, slug in heroes):
        raise SkillAplGenerationError('快照缺少可识别的英雄天赋树')
    filler = FILLER_CHOICES.get(spec)
    output = []
    compared = False
    appended_lists = []
    index = 0
    while index < len(lines):
        match = matches[index]
        group = match.group if match else None
        fury = spec == 'warrior_fury' and group in {'slayer', 'thane', 'slayer_aoe', 'thane_aoe'}
        if not match or group in COMMON or not fury and match.token not in samples:
            output.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines):
            other = matches[end]
            if not other or other.group != group or other.operator != '+=' or not fury and other.token not in samples:
                break
            end += 1
        entries = [(m.command, m.guard) for m in matches[index:end]]
        if (filler and group == filler[0] and entries[-1] == (filler[1], '')
                and filler[2] in samples and all(token != filler[2] for token, _ in entries)):
            entries.append((filler[2], filler[3]))
        lhs = 'actions' + ('.' + group if group else '')
        if fury:
            slug = 'slayer' if group.startswith('slayer') else 'mountain_thane'
            hero = next((i for i, name in heroes if name == slug), None)
            if hero is None:
                raise SkillAplGenerationError('狂暴专精默认 APL 与快照英雄树不匹配')
            counts = TARGETS[1:] if group.endswith('_aoe') else TARGETS[:1]
            by_sequence = defaultdict(list)
            for n in counts:
                sequence, valid = _fury_branch(entries, samples, hero, slug, n)
                compared |= valid
                by_sequence[tuple(sequence)].append(n)
            result = []
            if len(by_sequence) == 1:
                result = list(next(iter(by_sequence)))
            else:
                for sequence, tiers in by_sequence.items():
                    branch = f'wd_{group}_{tiers[0]}_{index}'
                    selector = _target_guard(tiers)
                    result.append((f'call_action_list,name={branch}', selector))
                    appended_lists.append('')
                    for j, (token, guard) in enumerate(sequence):
                        operator = '=' if j == 0 else '+='
                        appended_lists.append(f'actions.{branch}{operator}/' + _command(token, guard))
        elif len({t for t, _ in entries}) > 1:
            result, valid = _generic_block(entries, samples, heroes)
            compared |= valid
        else:
            result = _merged(entries)
        if result == [(m.command, m.guard) for m in matches[index:end]]:
            output.extend(lines[index:end])
        else:
            for j, (token, guard) in enumerate(result):
                operator = match.operator if j == 0 else '+='
                output.append(lhs + operator + '/' + _command(token, guard))
        index = end
    if not compared:
        raise SkillAplGenerationError('该专精的快照与默认 APL 缺少可比较的伤害技能')
    content = '\n'.join(output + appended_lists) + '\n'
    _, _, errors = validate_document(content)
    if errors:
        raise SkillAplGenerationError(f'生成的 APL 未通过结构校验，第 {errors[0].range.start.line} 行：{errors[0].message}')
    return content
