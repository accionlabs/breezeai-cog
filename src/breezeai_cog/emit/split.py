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
  The backend stores this id verbatim as ``captureId``, so the suffix survives to
  the MCP/agent read side; siblings share the same base and order by ``i``.
* Every part is flagged ``isPartial = True`` so a reader that encounters any one
  part (e.g. a lone search hit) knows the text is a fragment with siblings.
* Every part keeps the same ``parentId`` / ``nodeType`` / line span, so all parts
  attach to the same owner and sort together.
* Only the **first** part keeps the semantic classification (``semanticType`` plus
  the route/db/api/event fields) and any decorators; continuation parts carry
  overflow text only, so one source statement never fans out into ``n`` duplicate
  route/api graph nodes.
* Breaks fall on a newline boundary when one lands inside the window (keeps lines
  intact); an unbroken run longer than ``limit`` is hard-cut.

Lossless vs. capped
-------------------
Splitting is **lossless** — concatenating the parts reproduces the original text
byte-for-byte — *unless* ``max_parts`` is set and the statement would exceed it.
That cap is a backstop against Statement-node explosion from a pathological blob;
when it trips, the tail beyond ``max_parts`` parts is dropped, an inline
``…[+N chars dropped …]`` marker is appended to the last kept part so the loss is
honest (``isPartial`` alone would wrongly imply "reassemble for the whole"), and a
``statement.parts_capped`` warning is logged (id / path / parts / kept / dropped).
"""

from __future__ import annotations

from ..logging import get_logger
from ..schemas import Statement

log = get_logger("breezeai_cog.emit.split")

#: Optional single-hit classification fields — kept on part 1, cleared (→ null, dropped by
#: ``exclude_none``) on continuation parts so a split never multiplies route/api/db/event nodes.
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


def _cap_parts(pieces: list[str], full_len: int, max_parts: int) -> list[str]:
    """Enforce ``max_parts`` (``<= 0`` disables): keep the first ``max_parts`` pieces and
    append an honest ``…[+N chars dropped …]`` marker to the last kept one, trimming it if
    needed so it still fits the piece length. Returns ``pieces`` unchanged when within cap."""
    if max_parts <= 0 or len(pieces) <= max_parts:
        return pieces
    kept = pieces[:max_parts]
    dropped = full_len - sum(len(p) for p in kept)
    marker = f"…[+{dropped} chars dropped: max_statement_parts={max_parts}]"
    limit = len(pieces[0])  # first piece is a full window == the cap
    last = kept[-1]
    if len(last) + len(marker) > limit:
        last = last[: max(0, limit - len(marker))]
    kept[-1] = last + marker
    return kept


def split_oversized_statements(
    statements: list[Statement], limit: int, max_parts: int = 0
) -> list[Statement]:
    """Return ``statements`` with any whose ``text`` exceeds ``limit`` replaced by ordered
    ``#part{i}of{n}`` records (each flagged ``isPartial``). ``limit <= 0`` disables (returns
    as-is); ``max_parts > 0`` caps the part count (dropping + marking the tail). The common
    all-within-limit case returns the input list untouched (no copying)."""
    if limit <= 0 or all(len(s.text) <= limit for s in statements):
        return statements
    out: list[Statement] = []
    for st in statements:
        if len(st.text) <= limit:
            out.append(st)
            continue
        chunks = _chunks(st.text, limit)
        if 0 < max_parts < len(chunks):  # the cap will drop the tail — warn, don't do it silently
            dropped = len(st.text) - sum(len(p) for p in chunks[:max_parts])
            log.warning(
                "statement.parts_capped", id=st.id, path=st.path,
                parts=len(chunks), kept=max_parts, dropped_chars=dropped,
            )
        pieces = _cap_parts(chunks, len(st.text), max_parts)
        total = len(pieces)
        for idx, piece in enumerate(pieces, start=1):
            update: dict[str, object | None] = {
                "id": f"{st.id}#part{idx}of{total}", "text": piece, "isPartial": True,
            }
            if idx > 1:  # continuation: overflow text only, no duplicate semantic node
                update.update(dict.fromkeys(_CONTINUATION_CLEARED, None))
                update["decorators"] = []
            out.append(st.model_copy(update=update))
    return out
