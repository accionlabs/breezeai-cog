"""Walk a whole standalone ``.prisma`` document and emit flat ``Statement``s.

Covers the top-level Prisma Schema Language (PSL) blocks the ``prisma`` tree-sitter grammar
produces (verified empirically):

* **Models** — ``model User { … }`` → a ``data_model`` statement (``endpoint`` = the model
  name, the **full body** carried on ``text`` so every field, ``@relation``, and block
  attribute like ``@@map``/``@@index`` survives as text).
* **Enums** — ``enum Role { … }`` → one plain statement (real ``nodeType``, ``semanticType``
  null), the members carried on ``text``. Members are *not* split into their own records: the
  grammar turns an attributed member (``USER @map("user")``) into ``ERROR`` tokens mixed with
  the real members, so splitting would risk emitting an attribute token as a fabricated member
  — absent beats wrong. (This mirrors how the GraphQL walker treats an ``enum``.)
* **Config blocks** — ``datasource db { … }`` / ``generator client { … }`` → a plain
  statement (``endpoint`` = the block's instance name), the provider/url config on ``text``.

Every statement is flat and parented to the file (``parentId`` = ``file_id(path)``). Relations
between models (``author User @relation(…)``) are carried on the model's ``text`` and left to
the backend relationship phase to materialise — the parser never invents an edge (honest-null).

Not captured (honest gaps — the v13 grammar emits ``ERROR`` for these, so a type-based dispatch
skips them, never guessing): ``type X { … }`` composite types (MongoDB embedded documents) and
*empty* ``model``/``enum`` blocks.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text

#: Top-level blocks whose first child token is a fixed keyword (``datasource``/``generator``),
#: not part of the block's identity beyond marking it config.
_CONFIG_BLOCK = "key_value_block"


def _first_identifier(node: Node, src: bytes) -> str | None:
    """The block's declared name — its first ``identifier`` named child. For every PSL block
    (``model``/``enum``/``datasource``/``generator``) the name is the first named child; the
    ``datasource``/``generator`` keyword is an *unnamed* leading token, so it is skipped here."""
    ident = next((c for c in node.named_children if c.type == "identifier"), None)
    return node_text(ident, src) if ident is not None else None


def collect_prisma_statements(
    root: Node, source: bytes, path: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Walk a parsed Prisma document and emit one flat ``Statement`` per top-level block."""
    out: list[Statement] = []
    fid = file_id(path)

    def stmt(node: Node, name: str | None, **fields: object) -> Statement:
        line, col = node.start_point[0] + 1, node.start_point[1]
        return Statement(
            id=disambiguate(statement_id(path, line, col), seen_ids),
            parentId=fid,
            path=path,
            framework="prisma",
            nodeType=node.type,
            name=name,
            endpoint=name,  # the join key: a relation/reference resolves to a model by name
            text=node_text(node, source)[:limit],
            startLine=line,
            endLine=node.end_point[0] + 1,
            **fields,  # type: ignore[arg-type]
        )

    # PSL blocks are strictly top-level (they never nest), so a single pass over the
    # document's direct children is exhaustive — no recursion needed.
    for n in root.named_children:
        if n.type == "model_block":
            out.append(stmt(n, _first_identifier(n, source), semanticType="data_model"))
        elif n.type == "enum_block":
            out.append(stmt(n, _first_identifier(n, source)))
        elif n.type == _CONFIG_BLOCK:
            out.append(stmt(n, _first_identifier(n, source)))
        # anything else (comments handled by the shared pass; ERROR nodes for unsupported
        # `type`/empty blocks) is intentionally skipped.

    return out
