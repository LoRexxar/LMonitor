"""Resolve hero talent names from the final SimC talent build actually executed."""

import re

from botend.constants.wow import CLASS_SPEC_MAP
from botend.services.spec_stats_service import _hero_subtree_name_from_table
from botend.wow.talents.service import TalentBuildCodeService


_TALENTS_LINE_RE = re.compile(
    r'^\s*talents\s*=\s*["\']?([^\s#"\']+)["\']?\s*(?:#.*)?$',
    re.MULTILINE,
)


class HeroTalentAnalysisError(ValueError):
    """Raised when a talent build's selected hero subtree cannot be resolved."""


def extract_talent_code(simc_input):
    """Return the effective talent code from the final composed SimC input."""
    matches = _TALENTS_LINE_RE.findall(str(simc_input or ''))
    return matches[-1].strip() if matches else ''


def _simc_spec_identity(spec_key):
    normalized = str(spec_key or '').strip().lower()
    for class_name, spec_names in CLASS_SPEC_MAP.items():
        class_key = re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
        class_key = {
            'death_knight': 'deathknight',
            'demon_hunter': 'demonhunter',
        }.get(class_key, class_key)
        for spec_name in spec_names:
            normalized_spec = re.sub(r'(?<!^)(?=[A-Z])', '_', spec_name).lower()
            if normalized == f'{class_key}_{normalized_spec}':
                return class_name, spec_name
    return None


def resolve_hero_talent_names(
    talent_code,
    spec_key,
    *,
    use_ptr=False,
    build_api_view=None,
    hero_name_resolver=None,
):
    """Resolve selected hero subtree names for one explicit build code."""
    del use_ptr  # Simulator metadata version is selected by the authoritative usage resolver.
    talent_code = str(talent_code or '').strip()
    if not talent_code:
        raise HeroTalentAnalysisError('无法获取英雄天赋树：天赋字符串为空')

    identity = _simc_spec_identity(spec_key)
    if not identity:
        raise HeroTalentAnalysisError('无法获取英雄天赋树：专精无效')
    class_name, spec_name = identity

    try:
        parser = build_api_view or TalentBuildCodeService.build_api_view
        payload = parser(
            talent_build_code=talent_code,
            class_name=class_name,
            spec_name=spec_name,
            usage='simulator',
        )
        trees = (payload.get('talent_render_model') or {}).get('trees') or []
        resolver = hero_name_resolver or _hero_subtree_name_from_table
        names = []
        for tree in trees:
            if tree.get('tree_type') != 'hero':
                continue
            subtree_ids = {
                int(node.get('db2_subtree_id'))
                for node in (tree.get('nodes') or [])
                if node.get('tree_type') == 'hero'
                and node.get('db2_subtree_id') is not None
                and (node.get('selected') is True or int(node.get('points') or 0) > 0)
            }
            for subtree_id in sorted(subtree_ids):
                name = resolver(subtree_id)
                if name and name not in names:
                    names.append(name)
        if not names:
            raise HeroTalentAnalysisError('无法获取英雄天赋树：未解析到已选择的英雄天赋')
        return names
    except HeroTalentAnalysisError:
        raise
    except Exception as exc:
        raise HeroTalentAnalysisError(f'无法获取英雄天赋树：{exc}') from exc


def enrich_manifest_with_actual_hero_talents(
    manifest,
    simc_input,
    spec_key,
    *,
    use_ptr=False,
):
    """Attempt analysis once and freeze the outcome into a Run manifest."""
    frozen = dict(manifest or {})
    talent_code = extract_talent_code(simc_input)
    try:
        frozen['hero_talent_names'] = resolve_hero_talent_names(
            talent_code,
            spec_key,
            use_ptr=use_ptr,
        )
        frozen['hero_talent_analysis_error'] = ''
    except HeroTalentAnalysisError as exc:
        frozen['hero_talent_names'] = []
        frozen['hero_talent_analysis_error'] = str(exc)
    return frozen
