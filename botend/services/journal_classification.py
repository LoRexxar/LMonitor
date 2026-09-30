"""统一手册资料片与赛季标签，兼容未保存开放条件的旧发布。"""
from functools import lru_cache
import json
from pathlib import Path

from botend.services.journal_text import integer
from botend.services.season_keys import canonical_season_key


@lru_cache(maxsize=1)
def classification_reference():
    path = Path(__file__).resolve().parents[1] / 'data/journal_tier_conditions.json'
    return json.loads(path.read_text(encoding='utf-8'))


def instance_kind(instance_id, default):
    return classification_reference()['kind_overrides'].get(str(instance_id), {}).get('kind', default)


class JournalClassification:
    def __init__(self, release, season):
        self.manifest = release.manifest or {}
        self.season = season
        self.season_key = canonical_season_key(season.season_key) if season else ''
        reference = classification_reference()
        self.season_info = reference['seasons'].get(self.season_key, {})
        self.season_label = self.season_info.get('name') or ('当前活动赛季' if season else '')
        self.tiers = sorted(self.manifest['catalog']['tiers'], key=lambda row: -row['order'])
        self.current_ids = {row['id'] for row in self.tiers if row['order'] == 9000}
        self.retail_build = str(self.manifest.get('retail_build') or release.build.split('+ptr-', 1)[0])
        self.legacy_links = reference['legacy_links']

    def project(self, payload):
        """只生成展示投影，不更改不可变发布里的 tier_ids。"""
        iid = str(payload['id'])
        original = set(payload.get('tier_ids') or [])
        override = (self.manifest.get('display_overrides') or {}).get(iid) or {}
        overlay = (self.manifest.get('ptr_overlays') or {}).get(iid) or {}
        build = str(overlay.get('source_build') or self.retail_build)
        links = payload.get('tier_links')
        if links is None:
            links = self.legacy_links.get(build, {}).get(iid)
        if links is not None:
            current = any(
                integer(link['tier_id']) in self.current_ids and (
                    not integer(link.get('condition_id')) or
                    integer(link.get('condition_id')) == self.season_info.get('condition_id')
                ) for link in links
            )
        else:
            # 已知旧构建的条件目录是完整的；未知构建不可猜测条件含义。
            current = bool(original & self.current_ids) and build not in self.legacy_links
        if override:
            if self.season_key and canonical_season_key(override.get('season_key')) == self.season_key:
                current = True
            elif links is None:
                # 手工加上的本赛季标签到期后不能继续从旧 tier_ids 继承。
                current = False
        tier_ids = (original - self.current_ids) | (self.current_ids if current else set())
        historical = [row for row in self.tiers if row['id'] in tier_ids and row['order'] != 9000]
        return {**payload, 'kind': instance_kind(payload['id'], payload.get('kind')),
                'tier_ids': sorted(tier_ids),
                'tier_names': [row['name'] for row in reversed(historical)],
                'tier_name': ' / '.join(row['name'] for row in reversed(historical)),
                'is_current_season': current, 'season_label': self.season_label if current else ''}
