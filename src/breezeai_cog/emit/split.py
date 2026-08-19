"""Split oversized statement ``text`` into ordered part-records at emit time.

The backend rejects any statement whose ``text`` exceeds a size cap, so a single
huge statement — a captured JSON/data document, a generated-code blob, a giant
string literal — would be dropped **whole**. Rather than truncate and lose the
tail, we split it into ``n`` ordered parts, each within ``limit`` characters, so
nothing is lost and a reader can reconstruct the original by concatenating the
parts in order.

This is the *single* place statement text is sized: parsers extract full text;
this pass — run once at the pipeline emit choke point (``core.pipeline._assemble``)
— covers every producer (structured-json, route/event parsers, code statements)
uniformly, instead of each parser truncating on its own.

Convention
----------
* Each part's ``id`` gets a ``#part{i}of{n}`` suffix (``foo.json:1:0#part1of3``)
  so a reader — human or agent — sees the fragment belongs to a larger statement.
* Every part keeps the same ``parentId`` / ``nodeType`` / line span, so all parts
  attach to the same owner and sort together.
* Only the **first** part keeps the semantic classification (``semanticType`` plus
  the route/db/api/event fields); continuation parts carry overflow text only, so
  one source statement never fans out into ``n`` duplicate route/api graph nodes.
* Breaks fall on a newline boundary when one lands inside the window (keeps lines
  intact); an unbroken run longer than ``limit`` is hard-cut.
"""

from __future__ import annotations

from ..schemas import Statement

#: Semantic / classification fields that describe a *single* hit — kept on part 1,
#: cleared on continuation parts so a split never multiplies route/api/db/event nodes.
_CONTINUATION_CLEARED: tuple[str, ...] = (
    "semanticType", "name", "uiRole", "framework", "method", "endpoint",
    "handler", "handlerLine", "routeKind", "isRegex", "version", "authRequired",
    "guards", "requestDTO", "responseDTO", "dataLoaders", "dataAccessHint", "keyFields",
)


def _chunks(text: str, limit: int) -> list[str]:
    """Slice ``text`` into pieces each ``<= limit`` chars, breaking at the last
    newline inside the window when there is one (keeps lines whole), else hard-cut."""
    out: list[str] = []
    i, n = 0, len(text)
    while n - i > limit:
        window_end = i + limit
        nl = text.rfind("\n", i, window_end)
        cut = nl + 1 if nl > i else window_end  # include the newline; else hard-cut
        out.append(text[i:cut])
        i = cut
    out.append(text[i:])
    return out


def split_oversized_statements(statements: list[Statement], limit: int) -> list[Statement]:
    """Return ``statements`` with any whose ``text`` exceeds ``limit`` replaced by
    ordered ``#part{i}of{n}`` records. ``limit <= 0`` disables (returns as-is), and
    the common all-within-limit case returns the input list untouched (no copying)."""
    if limit <= 0 or all(len(s.text) <= limit for s in statements):
        return statements
    out: list[Statement] = []
    for st in statements:
        if len(st.text) <= limit:
            out.append(st)
            continue
        pieces = _chunks(st.text, limit)
        total = len(pieces)
        for idx, piece in enumerate(pieces, start=1):
            update: dict[str, object | None] = {"id": f"{st.id}#part{idx}of{total}", "text": piece}
            if idx > 1:  # continuation: overflow text only, no duplicate semantic node
                update.update(dict.fromkeys(_CONTINUATION_CLEARED, None))
            out.append(st.model_copy(update=update))
    return out
