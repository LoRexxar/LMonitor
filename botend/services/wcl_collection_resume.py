"""仅保存未确认样本的引用；事实、负缓存和重试期限仍由原有存储负责。"""
import hashlib
import json
import time

from botend.services.wcl_collection_cycle import fenced_cycle, frozen_combatant_records


CHECKPOINT_KEY = '_combatant_resume'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CombatantResume:
    def __init__(self, cycle):
        self.cycle = cycle
        self.references = {f'{"r" if name.startswith("raid:") else "d"}:{pk}'
                           for name, scopes in cycle.selected_samples.items()
                           for rows in scopes.values() for pk in rows}
        self.cohort = _digest([cycle.pk, cycle.season_id, cycle.selected_samples])
        self.remaining = set(self.references)
        checkpoint = (cycle.counts or {}).get(CHECKPOINT_KEY)
        if isinstance(checkpoint, dict):
            body = {key: checkpoint.get(key) for key in ('version', 'cohort', 'remaining')}
            refs = body['remaining']
            if (body['version'] == 1 and body['cohort'] == self.cohort
                    and isinstance(refs, list) and all(isinstance(ref, str) for ref in refs)
                    and len(refs) == len(set(refs)) and set(refs).issubset(self.references)
                    and checkpoint.get('checksum') == _digest(body)):
                self.remaining = set(refs)
        self.skipped = len(self.references) - len(self.remaining)

    def positive(self, rows):
        self.remaining.difference_update(row['_cycle_ref'] for row in rows)

    def counts(self, counts):
        control = {key: self.cycle.counts[key] for key in ('_ranking_pending', '_ranking_retry_at')
                   if key in (self.cycle.counts or {})}
        return {**control, **counts, 'selected': len(self.references),
                'cached': counts.get('cached', 0) + self.skipped,
                'resumed_cached': self.skipped}

    def checkpoint(self, counts):
        if self.remaining == self.references:
            return dict(counts)
        body = dict(version=1, cohort=self.cohort, remaining=sorted(self.remaining))
        return {**counts, CHECKPOINT_KEY: {**body, 'checksum': _digest(body)}}

    def save(self, counts):
        # 沿用采集器的 30 秒节流；事务失败最多重读末尾一批，不会跳过未提交事实。
        self.save_counts(self.checkpoint(self.counts(counts)))

    def save_counts(self, saved):
        with fenced_cycle(self.cycle, phases=('combatants',)) as current:
            current.counts = saved
            current.save(update_fields=['counts'])
            self.cycle.counts = saved


def collect_cycle_combatants(cycle, fetcher, *, log=None, admission_check=None):
    from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants

    resume = CombatantResume(cycle)
    records = frozen_combatant_records(cycle, references=resume.remaining)
    if log:
        log(f'WCL combatants resume selected={len(resume.references)} '
            f'resumed_cached={resume.skipped} checked={len(records)}')
    counts = collect_dungeon_combatants(
        records, fetcher, apply=True, batch_size=20, log=log, progress=resume.save,
        admission_check=admission_check, on_positive=resume.positive)
    counts = resume.counts(counts)
    if not counts.get('failed', 0) and not counts.get('pending', 0):
        if admission_check:
            admission_check()
        started = time.monotonic()
        # 完成屏障不能信任断点：全部引用必须存在，正向事实和负缓存必须仍然有效。
        records = frozen_combatant_records(cycle)
        resume.remaining = set(resume.references)
        verified = collect_dungeon_combatants(records, apply=False, on_positive=resume.positive)
        positive = verified.get('cached', 0)
        counts['matched'] = min(counts.get('matched', 0), positive)
        counts['cached'] = positive - counts['matched']
        for key in ('missing', 'permission_denied', 'pending', 'failed', 'negative_cached'):
            counts[key] = verified.get(key, 0)
        counts['verification_seconds'] = time.monotonic() - started
        counts['verification_fights'] = verified.get('preflight_fights', 0)
        if verified.get('retry_at'):
            counts['retry_at'] = verified['retry_at']
        if log:
            log(f'WCL combatants verification selected={len(records)} pending={counts["pending"]} '
                f'seconds={counts["verification_seconds"]:.3f}')
    if counts.get('failed', 0) or counts.get('pending', 0):
        counts = resume.checkpoint(counts)
    # 完成后移除临时控制引用，避免长期积累重复状态；核验结果仍需受所有权保护。
    resume.save_counts(counts)
    return counts
