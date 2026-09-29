"""Lucene.NET search-index access detection for C#.

A Lucene index is as load-bearing as a database, but every call into it lands as ordinary code:
``searcher.Search(q, 50)`` and ``writer.AddDocument(doc)`` are invisible as data access, so
"what touches the search index" has no answer.

**Additive** (like :mod:`lambda_events`): searching an index is an orthogonal capability, not a
framework identity. The same file is routinely an ASP.NET controller *and* a Lucene consumer, so
this runs inside ``CSharpParser.extract`` on top of whatever else the file is. A peer parser
claiming the file would displace the aspnet/wcf detector and lose the file's routes.

Precision gate: **the receiver's declared type, never the verb.** ``Search``, ``Add``, ``Open``,
``Count``, ``Commit``, ``Document`` and ``Explain`` are ordinary English words that appear on
caches, collections, HTTP clients and string builders — a verb table alone would light up every
repository in every language. C# states the type at the declaration site, and a declared type is
compiler-enforced rather than a naming convention, so only a receiver *declared* as a Lucene type
(or a static call on the type itself, ``DirectoryReader.Open``) is trusted. This mirrors the
``typed_db_ids`` gate ``typescript/statements.py`` applies to high-collision ORM verbs.

Both API generations are covered: 3.0.3 (``Optimize``, ``TermDocs``) and 4.8 (``ForceMerge``,
``Fields``), since a .NET Framework 4.6.1 codebase is almost certainly on 3.x.

Scope decisions (see ``.todo/lucene-net-detector-design.md``):

* **Only the calls that touch the index are marked.** Building a query (``parser.Parse(...)``,
  ``new BooleanQuery { … }``) and building a document (``doc.Add(new Field(...))``) stay ordinary
  statements — their text is captured and searchable, but they are not data access. This matches
  Elasticsearch, where the DSL is the statement's text and the call is the record.
* **No ``dataAccessHint``.** The spec's enum has no ``lucene``, and the near values would be
  wrong: ``elasticsearch`` names a different product, and ``orm`` means "product not establishable
  from this file" when here it is. The record still says "this line reads the index" and carries
  the invoked method, so the access stops being invisible; adding the enum value is a separate
  request.
"""

from __future__ import annotations

from typing import Iterator

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

#: Cheap byte guard — the namespace every Lucene.NET file imports.
_MARKER = b"Lucene.Net"

#: Types whose methods reach the index. A receiver declared as one of these is trusted; so is a
#: static call on the type itself (``DirectoryReader.Open(dir)``).
_INDEX_TYPES = frozenset({
    # searchers
    "IndexSearcher", "MultiSearcher", "ParallelMultiSearcher", "SearcherManager",
    # writers
    "IndexWriter", "TrackingIndexWriter",
    # readers
    "IndexReader", "DirectoryReader", "AtomicReader", "CompositeReader", "MultiReader",
})

#: Read side — searching and fetching stored documents.
_READ_VERBS = frozenset({
    "Search", "SearchAfter", "Doc", "Document", "Explain", "Count",
    "TermDocs", "TermPositions", "Terms", "Fields", "GetTermVector", "GetTermVectors",
    "Open", "OpenIfChanged", "Acquire",
})

#: Write side — mutating the index. ``Optimize`` is 3.x, ``ForceMerge`` its 4.x replacement.
_WRITE_VERBS = frozenset({
    "AddDocument", "AddDocuments", "UpdateDocument", "UpdateDocuments",
    "DeleteDocuments", "DeleteAll", "AddIndexes",
    "Commit", "PrepareCommit", "Rollback", "Flush", "Optimize", "ForceMerge",
})

_VERBS = _READ_VERBS | _WRITE_VERBS

#: Declaration nodes that state a variable's type. Two shapes, confirmed by dumping the grammar:
#: a ``variable_declaration`` (field or local) names the variable inside a ``variable_declarator``,
#: while a ``parameter`` or ``property_declaration`` carries two bare identifiers.
_DECL_NODES = frozenset({"variable_declaration", "parameter", "property_declaration"})


def _iter(root: Node, kinds: frozenset[str] | set[str]) -> Iterator[Node]:
    """Every descendant of ``root`` whose type is in ``kinds``."""
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type in kinds:
            yield node


def _declared_index_names(root: Node, source: bytes) -> set[str]:
    """Identifiers declared as a Lucene index type.

    Reads the declaration's own tokens — ``IndexSearcher _searcher`` yields ``_searcher`` — so the
    signal is the compiler's, not a guess from the name. A declaration whose type is unrelated
    contributes nothing, which is what keeps the verb table from firing on look-alikes.
    """
    names: set[str] = set()
    for decl in _iter(root, _DECL_NODES):
        kids = list(decl.named_children)
        for i, kid in enumerate(kids[:-1]):
            if kid.type != "identifier" or node_text(kid, source) not in _INDEX_TYPES:
                continue
            following = kids[i + 1]
            if following.type == "variable_declarator":  # field / local: the name is nested
                name_node = next(
                    (c for c in following.named_children if c.type == "identifier"), None)
                if name_node is not None:
                    names.add(node_text(name_node, source))
            elif following.type == "identifier":         # parameter / property: a bare identifier
                names.add(node_text(following, source))
            break
    return names


def _call_parts(call: Node, source: bytes) -> tuple[str, str] | None:
    """``(receiver, method)`` for ``x.Foo(...)``, else None for a bare or chained call."""
    function_node = call.child_by_field_name("function")
    if function_node is None or function_node.type != "member_access_expression":
        return None
    parts = [c for c in function_node.named_children if c.type == "identifier"]
    if len(parts) < 2:
        return None
    return node_text(parts[0], source), node_text(parts[-1], source)


def _owner_id(line: int, record: FileRecord, path: str) -> str:
    """The capture id of the innermost function containing ``line``, else the file.

    A statement parents to its most specific owner; the enclosing function is found by line
    containment, which is how the model expresses nesting everywhere else.
    """
    best: tuple[int, str] | None = None
    for fn in record.functions:
        if fn.startLine <= line <= fn.endLine:
            span = fn.endLine - fn.startLine
            if best is None or span < best[0]:
                best = (span, fn.id)
    return best[1] if best is not None else file_id(path)


def detect_lucene_access(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Mark Lucene index reads and writes as data access. Returns whether any were found.

    Appends one record per qualifying call, keeping the call's real ``nodeType`` — there is a
    backing AST node, so nothing is synthetic. A second record on a line the base parser already
    captured is the established shape for a line that carries two facts (``semanticType`` holds
    one value); see ``.todo/statement-id-position-collisions.md``.
    """
    if _MARKER not in source:
        return False
    declared = _declared_index_names(root, source)
    if not declared and not any(t.encode() in source for t in _INDEX_TYPES):
        return False

    seen = {s.id for s in record.statements}
    found = False
    for call in _iter(root, {"invocation_expression"}):
        parts = _call_parts(call, source)
        if parts is None:
            continue
        receiver, method = parts
        if method not in _VERBS:
            continue
        # Trusted only on a receiver whose declared type is a Lucene index type, or on the type
        # itself for a static factory call.
        if receiver not in declared and receiver not in _INDEX_TYPES:
            continue
        line, col = call.start_point[0] + 1, call.start_point[1]
        new_id = disambiguate(statement_id(path, line, col), seen)
        seen.add(new_id)
        record.statements.append(Statement(
            id=new_id,
            parentId=_owner_id(line, record, path),
            nodeType=call.type,
            semanticType="db_method_call",
            text=first_line(node_text(call, source))[:120],
            method=method,
            startLine=line,
            endLine=call.end_point[0] + 1,
            path=path,
        ))
        found = True
    return found
