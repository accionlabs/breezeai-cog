"""Shared, language-agnostic comment capture (gated by --capture-statements).

Every parser runs one whole-file pass over its AST that turns source **comments** into
flat :class:`~breezeai_cog.schemas.Statement` records — each keeping its real tree-sitter
``nodeType`` (``comment`` / ``line_comment`` / ``block_comment`` / …), tagged
``semanticType="comment"`` — so a comment becomes embedded, scoped and searchable with no
backend change (statements are already embedded and carry parent scope). This lives here —
not bolted onto the per-scope ``extract_statements`` call sites — because those have gaps
(no file-root capture in C++/C#/Java/Kotlin/VB, no class-body in C++/VB, and Python attaches
a leading class/function comment outside the block). A single root-level walk sidesteps
every one of those uniformly.

The pass is driven entirely by **line numbers from records the parser already built** — the
``(startLine, endLine, id)`` spans of every Function + Class, and the line ranges of emitted
statements — so it needs no per-grammar declaration node types. Only the set of comment node
types varies per language (``COMMENT_TYPES`` in each ``mappings.py``): most grammars use
``comment``; Java/Kotlin/Groovy split into ``line_comment`` / ``block_comment`` /
``multiline_comment`` / ``groovydoc_comment``.

Two rules:

* **Binding** (which node owns a comment), in priority order:
    1. *bind-ahead* — the nearest Function/Class starting at/after the comment, provided no
       emitted statement sits between them AND that scope lies within the comment's own
       containing scope (so a decorated/annotated declaration's doc-comment binds to it,
       while a trailing note at the tail of a scope does not leak onto the next sibling).
    2. *containment* — else the innermost Function/Class whose span contains the comment.
    3. *file* — else the file.
* **Dedup** ("except if captured in the parent's text field") — skip a comment whose line
  range is contained in an **absorbing span**: an emitted statement whose ``nodeType`` is not
  control-flow. Control-flow statements keep only their first line as ``text`` yet span their
  whole body, so they are excluded — otherwise an in-body comment would be wrongly dropped.

Consecutive standalone single-line comments are merged into one record (fixes multi-line
``//``/``#`` preambles being split by the grammar).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator

from tree_sitter import Node

from ..emit import disambiguate, statement_id
from ..schemas import Statement
from ..utils import truncate

# (startLine, endLine, id) for a Function/Class scope.
ScopeSpan = tuple[int, int, str]


def _iter_comment_nodes(root: Node, comment_types: Collection[str]) -> Iterator[Node]:
    """Yield every comment node in the tree (comments are ``extra`` nodes that attach as
    named children wherever they occur, so a plain named-child walk reaches them)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in comment_types:
            yield node
        stack.extend(node.named_children)


def _line_prefix_blank(node: Node, source: bytes) -> bool:
    """True if only whitespace precedes ``node`` on its line — i.e. it is a *standalone*
    comment (own line), not a trailing one after code (``x = 1  // note``)."""
    nl = source.rfind(b"\n", 0, node.start_byte)
    return source[nl + 1 : node.start_byte].strip() == b""


class _Group:
    """A merged comment (one or more contiguous standalone single-line comments, or a lone
    block comment). ``first``/``last`` bound its source span."""

    __slots__ = ("first", "last", "single", "standalone")

    def __init__(self, node: Node, *, single: bool, standalone: bool) -> None:
        self.first = self.last = node
        self.single = single
        self.standalone = standalone

    def mergeable_with(self, node: Node, single: bool, standalone: bool) -> bool:
        return (
            self.single
            and self.standalone
            and single
            and standalone
            and node.start_point[0] == self.last.end_point[0] + 1
        )


def _merge(comments: list[Node], source: bytes) -> list[_Group]:
    """Group comments (already position-sorted): runs of contiguous standalone single-line
    comments coalesce; block comments and trailing comments stand alone."""
    groups: list[_Group] = []
    cur: _Group | None = None
    for node in comments:
        single = node.start_point[0] == node.end_point[0]
        standalone = _line_prefix_blank(node, source)
        if cur is not None and cur.mergeable_with(node, single, standalone):
            cur.last = node
        else:
            cur = _Group(node, single=single, standalone=standalone)
            groups.append(cur)
    return groups


def _within(inner: tuple[int, int], outer: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _containing(cs: int, ce: int, scopes: list[ScopeSpan]) -> ScopeSpan | None:
    """Innermost scope whose span contains ``[cs, ce]`` (innermost = latest start)."""
    best: ScopeSpan | None = None
    for span in scopes:
        s, e, _ = span
        if s <= cs and ce <= e and (best is None or s > best[0]):
            best = span
    return best


def _bind_ahead(
    ce: int,
    scopes_by_start: list[ScopeSpan],
    stmt_starts: list[int],
    container: ScopeSpan | None,
) -> str | None:
    """The id of the nearest scope this comment documents from above, or ``None``.

    The candidate is the nearest scope starting at/after the comment. It is rejected if it
    escapes the comment's own containing scope (a tail comment must not leak onto the next
    sibling) or if an emitted statement lies between the comment and it (then the comment
    documents that statement, not the scope). Decorators/annotations are not emitted as
    statements, so they never block a real doc-comment binding."""
    for s, e, sid in scopes_by_start:  # ascending by start line
        if s < ce:
            continue
        if container is not None and not _within((s, e), (container[0], container[1])):
            return None
        if any(ce < st < s for st in stmt_starts):
            return None
        return sid
    return None


def comment_statements_for(
    root: Node,
    source: bytes,
    path: str,
    *,
    file_id: str,
    functions: list,
    classes: list,
    statements: list,
    control_flow: Collection[str],
    comment_types: Collection[str],
    limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    """Convenience wrapper for a parser's ``extract``: derive the scope table, statement start
    lines, and non-control-flow absorbing spans from the records the parser already built, then
    run the comment pass. Call after ``functions``/``classes``/``statements`` are assembled and
    only when ``--capture-statements`` is on."""
    scope_spans: list[ScopeSpan] = [(f.startLine, f.endLine, f.id) for f in functions]
    scope_spans += [(c.startLine, c.endLine, c.id) for c in classes]
    # Absorbing spans = where a comment already lives in a statement's ``text`` (so it must not
    # also become its own node). A non-control-flow statement absorbs its whole line range
    # (interior + folded same-line trailing comment); a control-flow statement absorbs only its
    # header line (its ``text`` is ``first_line`` — a comment on the header rides along, but
    # in-body comments on later lines must still be captured).
    absorbing = [(s.startLine, s.endLine) for s in statements if s.nodeType not in control_flow]
    absorbing += [(s.startLine, s.startLine) for s in statements if s.nodeType in control_flow]
    return collect_comment_statements(
        root, source, path, file_id=file_id, scope_spans=scope_spans,
        stmt_start_lines=[s.startLine for s in statements],
        absorbing_spans=absorbing,
        comment_types=comment_types, limit=limit, seen_ids=seen_ids,
    )


def collect_comment_statements(
    root: Node,
    source: bytes,
    path: str,
    *,
    file_id: str,
    scope_spans: list[ScopeSpan],
    stmt_start_lines: Collection[int],
    absorbing_spans: Collection[tuple[int, int]],
    comment_types: Collection[str],
    limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    """Emit one comment ``Statement`` per (merged) source comment, scoped by the binding
    rule and deduped against ``absorbing_spans``. See the module docstring."""
    comment_nodes = sorted(
        _iter_comment_nodes(root, comment_types),
        key=lambda n: (n.start_point[0], n.start_point[1]),
    )
    if not comment_nodes:
        return []

    scopes_by_start = sorted(scope_spans, key=lambda sp: sp[0])
    stmt_starts = sorted(stmt_start_lines)
    absorbing = list(absorbing_spans)

    out: list[Statement] = []
    for grp in _merge(comment_nodes, source):
        cs = grp.first.start_point[0] + 1
        ce = grp.last.end_point[0] + 1
        # Dedup: already inside a captured (non-control-flow) statement's text.
        if any(_within((cs, ce), span) for span in absorbing):
            continue
        container = _containing(cs, ce, scope_spans)
        parent = _bind_ahead(ce, scopes_by_start, stmt_starts, container)
        if parent is None:
            parent = container[2] if container is not None else file_id
        col = grp.first.start_point[1]
        text = source[grp.first.start_byte : grp.last.end_byte].decode("utf-8", "replace")
        out.append(
            Statement(
                id=disambiguate(statement_id(path, cs, col), seen_ids),
                parentId=parent,
                nodeType=grp.first.type,  # actual tree-sitter type (comment/line_comment/…)
                semanticType="comment",
                text=truncate(text, limit),
                startLine=cs,
                endLine=ce,
                path=path,
            )
        )
    return out
