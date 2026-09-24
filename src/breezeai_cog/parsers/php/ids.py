"""PHP statement-span tracking for route and hook deduplication."""

from __future__ import annotations

from typing import Any


class SeenIds(set[str]):
    """Tracks seen statement IDs for deduplication and statement records by span."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.by_span: dict[tuple[str, int, int], Any] = {}

    def register(self, fid: str, start_byte: int, end_byte: int, record: Any) -> None:
        self.by_span[(fid, start_byte, end_byte)] = record

    def get_record(self, fid: str, start_byte: int, end_byte: int) -> Any | None:
        return self.by_span.get((fid, start_byte, end_byte))

    def find_by_span(
        self, fid: str, start_byte: int, end_byte: int, node: Any | None = None
    ) -> Any | None:
        rec = self.by_span.get((fid, start_byte, end_byte))
        if rec is not None:
            return rec
        if node is not None:
            p = getattr(node, "parent", None)
            while p is not None:
                rec = self.by_span.get(
                    (fid, getattr(p, "start_byte", -1), getattr(p, "end_byte", -1))
                )
                if rec is not None:
                    return rec
                if getattr(p, "type", None) == "expression_statement":
                    break
                p = getattr(p, "parent", None)
        return None


def register_statement_span(
    seen_ids: Any, fid: str, start_byte: int, end_byte: int, record: Any
) -> None:
    """Register a statement record under its source span."""
    if hasattr(seen_ids, "register"):
        seen_ids.register(fid, start_byte, end_byte, record)
    elif hasattr(seen_ids, "by_span") and isinstance(seen_ids.by_span, dict):
        seen_ids.by_span[(fid, start_byte, end_byte)] = record
