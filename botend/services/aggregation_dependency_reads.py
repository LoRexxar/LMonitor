"""一次依赖校验内按身份复用中央资料；不跨任务、不落盘、不写数据库。"""
import sys


class DependencyReads:
    """仅保存已实际查询的资料和缺项；预算耗尽时照常查询，不牺牲正确性。"""

    def __init__(self, max_bytes=16 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.used_bytes = 0
        self.entries = {}

    def _remember(self, key, value):
        size, seen, pending = 128, set(), [key, value]
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            size += sys.getsizeof(item)
            if self.used_bytes + size > self.max_bytes:
                return
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set)):
                pending.extend(item)
            elif hasattr(item, '__dict__'):
                pending.append(vars(item))
        self.entries[key] = value
        self.used_bytes += size

    def many(self, namespace, identities, load):
        identities = sorted(set(identities))
        result, missing = {}, []
        for identity in identities:
            key = (namespace, identity)
            if key in self.entries:
                result[identity] = self.entries[key]
            else:
                missing.append(identity)
        if missing:
            loaded = load(missing)
            for identity in missing:
                value = loaded.get(identity)
                result[identity] = value
                self._remember((namespace, identity), value)
        return {key: value for key, value in result.items() if value is not None}


def dependency_reads(context):
    if 'dependency-reads' not in context:
        context['dependency-reads'] = DependencyReads()
    return context['dependency-reads']
