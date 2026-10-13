"""The shared output rules for detectors (parsers/emits.py): enrich or append, once per run,
ids, parents."""

from __future__ import annotations

from breezeai_cog.parsers.emits import Emit, apply
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.schemas import FileRecord, Function, Statement

SOURCE = b"""function main() {
  app.get('/a', h).post('/b', h);
}
"""


def _calls():
    root = parse_source("typescript", SOURCE, 0).root_node
    found = []

    def walk(n):
        if n.type == "call_expression":
            found.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    # Outer call first: app.get(...).post(...), then app.get(...).
    return found


def _record(*, base: str | None = None) -> FileRecord:
    record = FileRecord(id="a.ts", path="a.ts", type="code", language="typescript", loc=3)
    record.functions.append(Function(
        id="a.ts#main", parentId="a.ts", name="main", type="function", startLine=1, endLine=3,
    ))
    record.statements.append(Statement(
        id="a.ts:2:2", parentId="a.ts#main", nodeType="expression_statement",
        text="app.get('/a', h).post('/b', h);", startLine=2, endLine=2, semanticType=base,
    ))
    return record


def test_enrich_writes_fields_and_fill_only_when_truthy() -> None:
    post, _ = _calls()
    record = _record()
    apply(record, [Emit(post, {"semanticType": "route", "endpoint": "/b"},
                        fill={"method": "POST", "handler": None}, enrich="any")], SOURCE, "a.ts")
    [s] = record.statements
    assert (s.semanticType, s.endpoint, s.method, s.handler) == ("route", "/b", "POST", None)


def test_a_statement_is_enriched_once_per_run() -> None:
    post, get = _calls()
    record = _record()
    apply(record, [
        Emit(post, {"semanticType": "route", "endpoint": "/b"}, enrich="any"),
        Emit(get, {"semanticType": "route", "endpoint": "/a"}, enrich="any"),
    ], SOURCE, "a.ts")
    base, added = record.statements
    assert base.endpoint == "/b"
    # The second route on the same statement gets its own record, at its own column, parented
    # to the enclosing function.
    assert (added.id, added.endpoint, added.parentId) == ("a.ts:2:2#2", "/a", "a.ts#main")
    assert added.nodeType == "call_expression" and added.text == "app.get('/a', h)"


def test_unclassified_only_enriches_an_unclassified_statement() -> None:
    post, _ = _calls()
    record = _record(base="db_method_call")
    apply(record, [Emit(post, {"semanticType": "api_call"}, enrich="unclassified")], SOURCE, "a.ts")
    assert [s.semanticType for s in record.statements] == ["db_method_call", "api_call"]

    record = _record(base="db_method_call")
    apply(record, [Emit(post, {"semanticType": "api_call", "dataAccessHint": None},
                        enrich="unclassified", reclassify=frozenset({"db_method_call"}))],
          SOURCE, "a.ts")
    assert [s.semanticType for s in record.statements] == ["api_call"]


def test_never_appends_with_overrides() -> None:
    post, _ = _calls()
    record = _record()
    apply(record, [Emit(post, {"semanticType": "route"}, parent="a.ts", text="custom",
                        node_type="synthetic", line=2, col=0, end_line=3)], SOURCE, "a.ts")
    added = record.statements[-1]
    assert (added.id, added.parentId, added.text, added.nodeType, added.endLine) == (
        "a.ts:2:0", "a.ts", "custom", "synthetic", 3,
    )


def test_ids_are_disambiguated_in_emit_order() -> None:
    post, _ = _calls()
    record = _record()
    apply(record, [Emit(post, {"endpoint": e}, col=2) for e in ("/1", "/2")], SOURCE, "a.ts")
    assert [(s.id, s.endpoint) for s in record.statements[1:]] == [
        ("a.ts:2:2#2", "/1"), ("a.ts:2:2#3", "/2"),
    ]


def test_own_enriches_only_the_construct_s_own_statement() -> None:
    post, get = _calls()
    # The statement spans line 2 — the outer call's own span — so it may be enriched by it…
    record = _record()
    apply(record, [Emit(post, {"semanticType": "navigation"}, enrich="own")], SOURCE, "a.ts")
    assert [s.semanticType for s in record.statements] == ["navigation"]
    # …but not by a construct whose span differs (here: an emit pinned to other lines).
    record = _record()
    apply(record, [Emit(get, {"semanticType": "navigation"}, enrich="own", end_line=3)],
          SOURCE, "a.ts")
    assert [s.semanticType for s in record.statements] == [None, "navigation"]


def test_starts_enriches_the_statement_the_construct_begins() -> None:
    post, get = _calls()
    # A construct on the statement's first line enriches it, whatever the spans' ends…
    record = _record()
    apply(record, [Emit(get, {"semanticType": "eventbus_send"}, enrich="starts", end_line=3)],
          SOURCE, "a.ts")
    assert [s.semanticType for s in record.statements] == ["eventbus_send"]
    # …one on a later line of a statement (an argument on its second line) does not.
    record = _record()
    record.statements[0].endLine = 3
    apply(record, [Emit(get, {"semanticType": "eventbus_send"}, enrich="starts", line=3)],
          SOURCE, "a.ts")
    assert [(s.startLine, s.semanticType) for s in record.statements] == [
        (2, None), (3, "eventbus_send")]
