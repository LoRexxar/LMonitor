from __future__ import annotations

from typing import Any


class WagoDB2Client:
    """Small DB2 data access wrapper.

    The default implementation delegates to a provider object when available. This keeps the
    graph service independent from WagoSkillDiffMonitor while allowing the current monitor to
    reuse its bounded HTTP methods during report generation.
    """

    def __init__(self, *, build: str = '', locale: str = '', provider: Any = None):
        self.build = str(build or '')
        self.locale = str(locale or '')
        self.provider = provider

    def get_row_by_id(self, table: str, record_id: int) -> dict[str, Any]:
        if self.provider and hasattr(self.provider, '_fetch_db2_row_by_id'):
            old_locale = getattr(self.provider, 'locale', None)
            try:
                if self.locale and old_locale is not None:
                    self.provider.locale = self.locale
                row = self.provider._fetch_db2_row_by_id(table, self.build, record_id)
                return row if isinstance(row, dict) else {}
            finally:
                if old_locale is not None:
                    self.provider.locale = old_locale
        return {}

    def get_rows_by_ids(self, table: str, record_ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for rid in record_ids or []:
            try:
                rid_int = int(rid or 0)
            except (TypeError, ValueError):
                continue
            if rid_int <= 0:
                continue
            out[rid_int] = self.get_row_by_id(table, rid_int)
        return out

    def get_rows_by_field(self, table: str, field: str, value: Any) -> list[dict[str, Any]]:
        # Generic field search can be added with Wago filters or local CSV dumps later. Keep the
        # method in the public API so resolvers and tests depend on a stable interface.
        return []
