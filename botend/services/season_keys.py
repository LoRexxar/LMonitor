"""魔兽赛季标识的统一规范化规则。"""

from __future__ import annotations

import re


EXPANSION_ALIASES = {
    'midnight': 'mn',
    'the-war-within': 'tww',
    'war-within': 'tww',
}


def canonical_season_key(value='', *, season_name='', short_name='', rio_season=''):
    """把各上游对同一赛季的命名统一为 ``{资料片缩写}-s{序号}``。"""
    candidates = (rio_season, value, short_name)
    for candidate in candidates:
        normalized = re.sub(r'[^a-z0-9]+', '-', str(candidate or '').strip().lower()).strip('-')
        if not normalized:
            continue
        match = re.fullmatch(r'season-([a-z]+)-(\d+)', normalized)
        if match:
            return f'{EXPANSION_ALIASES.get(match.group(1), match.group(1))}-s{match.group(2)}'
        match = re.fullmatch(r'([a-z][a-z0-9-]*?)-s(\d+)', normalized)
        if match:
            expansion = EXPANSION_ALIASES.get(match.group(1), match.group(1))
            return f'{expansion}-s{match.group(2)}'

    normalized_name = re.sub(
        r'[^a-z0-9]+', '-', str(season_name or '').strip().lower(),
    ).strip('-')
    match = re.fullmatch(r'(.+?)-season-(\d+)', normalized_name)
    if match:
        expansion = EXPANSION_ALIASES.get(match.group(1), match.group(1))
        return f'{expansion}-s{match.group(2)}'
    return str(value or short_name or '').strip().lower() or 'current-season'
