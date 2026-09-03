"""JsonParser: routing (full capture vs config record), whole-document TOON capture, the
gated single `structured_data` statement, credential redaction, lossless capture, and single
ownership of `.json`."""

from __future__ import annotations

import json

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.structured_json.parser import JsonParser


def _parse(
    path: str,
    obj,
    *,
    capture_statements: bool = False,
    statement_text_limit: int = 8000,
):
    src = json.dumps(obj, ensure_ascii=False).encode()
    ctx = ParseContext(
        path=path,
        abs_path=None,
        source=src,
        repo_root=".",
        capture_statements=capture_statements,
        statement_text_limit=statement_text_limit,
    )
    return JsonParser().parse_file(ctx)


def _toon(path: str, obj, **kw) -> str:
    # TOON lives on the (gated) statement, not on metadata — capture it and read its text
    return _parse(path, obj, capture_statements=True, **kw).statements[0].text


def _language(path: str, obj) -> str:
    return _parse(path, obj).language


# ── routing: full capture ("structured-json") vs reduced/rich config ("config") ─
def test_captures_record_array() -> None:
    assert _language("x.json", {"values": [{"code": "REC-1"}]}) == "structured-json"
    assert _language("x.json", [{"id": 1}, {"id": 2}]) == "structured-json"


def test_captures_flat_map_and_scalar_array() -> None:
    # shape-independent: flat config, uniform map, and root scalar array all captured in full
    assert _language("x.json", {"prefix": "http://"}) == "structured-json"
    assert _language("x.json", {"a": "1", "b": "2"}) == "structured-json"
    assert _language("x.json", ["a", "b", "c"]) == "structured-json"


def test_named_rich_configs_stay_config() -> None:
    # only the NAME excludes — a record array inside package.json/tsconfig never diverts them
    assert _language("package.json", {"name": "app", "dependencies": {"react": "^18"}}) == "config"
    assert _language("package.json", {"name": "app", "contributors": [{"name": "Al"}]}) == "config"
    assert _language("tsconfig.json", {"compilerOptions": {}, "references": [{"path": "../x"}]}) == "config"
    assert _language("mod.json", {"main": "Verticle"}) == "config"


def test_composer_json_is_captured_not_named_rich() -> None:
    # composer.json has no dedicated extractor → captured in full (only RICH_JSON_NAMES are config)
    assert _language("composer.json", {"name": "v/p", "authors": [{"name": "X"}]}) == "structured-json"


def test_empty_and_scalar_are_config() -> None:
    assert _language("x.json", {}) == "config"
    assert _language("x.json", []) == "config"
    assert _language("x.json", "hello") == "config"


def test_malformed_json_routes_to_config() -> None:
    # unparseable → nothing to capture → the shared config extractor (parseError metadata)
    ctx = ParseContext(path="x.json", abs_path=None, source=b"{not json", repo_root=".")
    assert JsonParser().parse_file(ctx).language == "config"


# ── emission: capture record, no Function/Class nodes ───────────────────────────
def test_emits_config_record_no_functions() -> None:
    rec = _parse("json/records.json", {"values": [{"code": "REC-1"}]})
    assert rec.type == "config" and rec.language == "structured-json"
    assert rec.functions == [] and rec.classes == []  # never mints graph-function noise
    assert rec.metadata["kind"] == "structured-json"
    assert rec.metadata["format"] == "toon"


def test_toon_tabular_for_uniform_records() -> None:
    # uniform array-of-objects → columns declared once, one comma-joined row per record
    rec = _parse(
        "t.json",
        {
            "team": "pay",
            "members": [{"name": "Al", "role": "admin"}, {"name": "Bo", "role": "viewer"}],
        },
        capture_statements=True,
    )
    toon = rec.statements[0].text
    assert "team: pay" in toon
    assert "members[2]{name,role}:" in toon
    assert "  Al,admin" in toon
    assert "  Bo,viewer" in toon
    assert rec.metadata["recordCount"] == 2
    assert "toon" not in rec.metadata  # content is on the statement, not metadata


def test_toon_block_form_for_records_with_nested_objects() -> None:
    # a nested object in a record → not a flat table → canonical block form (no dot-folding)
    toon = _toon(
        "t.json",
        {
            "members": [
                {"name": "Al", "profile": {"level": 3}},
                {"name": "Bo", "profile": {"level": 1}},
            ]
        },
    )
    assert "members[2]:" in toon
    assert "- name: Al" in toon and "level: 3" in toon


def test_toon_block_form_for_non_uniform_records() -> None:
    # differing nested keys (validation vs source) → not tabular → block form with `- ` items
    toon = _toon(
        "t.json",
        {
            "fields": [
                {"id": "a", "validation": {"required": True}},
                {"id": "b", "source": {"table": "M_DEPT"}},
            ]
        },
    )
    assert "fields[2]:" in toon
    assert "- id: a" in toon and "- id: b" in toon


def test_toon_root_array_of_records() -> None:
    toon = _toon("t.json", [{"id": 1, "k": "x"}, {"id": 2, "k": "y"}])
    assert toon.startswith("[2]{id,k}:")


def test_null_rendered_as_null() -> None:
    # present-but-null stays in the row as the canonical TOON `null` (distinct from absent)
    toon = _toon("a.json", [{"code": "REC", "joins": None}])
    assert "[1]{code,joins}:" in toon
    assert "REC,null" in toon


def test_credential_keys_redacted_including_nested() -> None:
    # nested `creds` → block form; secret leaves (password, apiKey) redacted before encoding
    toon = _toon("a.json", [{"user": "bob", "password": "hunter2", "creds": {"apiKey": "sk-1"}}])
    assert "password: ***" in toon and "apiKey: ***" in toon
    assert "hunter2" not in toon and "sk-1" not in toon


def test_long_value_survives_uncapped() -> None:
    # No per-value cap: a ~250k-char leaf is captured byte-for-byte, with no ellipsis truncation.
    long = "x" * 250_000
    toon = _toon("a.json", [{"blob": long}])
    assert long in toon
    assert "…" not in toon


def test_large_document_keeps_every_leaf_no_truncation() -> None:
    # No leaf cap: a large document is captured in full (leafCount == every leaf) and never
    # flagged truncated — oversized TOON is split into parts downstream (emit.split), not dropped.
    big = [{"n": i} for i in range(6000)]
    rec = _parse("a.json", big)
    assert rec.metadata["leafCount"] == 6000
    assert "truncated" not in rec.metadata


def test_every_leaf_marker_reaches_toon() -> None:
    # distinct markers m0..m5999 each survive to the TOON — no leaf silently dropped
    toon = _toon("a.json", [{"v": f"m{i}"} for i in range(6000)])
    rows = {line.strip() for line in toon.splitlines()}
    assert all(f"m{i}" in rows for i in range(6000))


# ── the single structured_data statement (gated behind --capture-statements) ─────
def test_no_statement_without_capture_statements() -> None:
    # without --capture-statements the content is not captured anywhere (no statement, and
    # metadata carries only the structural summary — never the TOON content)
    rec = _parse("a.json", [{"id": 1}], capture_statements=False)
    assert rec.statements == []
    assert "toon" not in rec.metadata
    assert rec.metadata["recordCount"] == 1


def test_document_statement_shape() -> None:
    rec = _parse("cfg/team.json", {"members": [{"name": "Al"}]}, capture_statements=True)
    assert len(rec.statements) == 1
    st = rec.statements[0]
    assert st.nodeType == "synthetic"
    assert st.semanticType == "structured_data"
    assert st.framework == "toon"
    assert st.endpoint is None  # no address/target for a data document (honest-null)
    assert st.name == "team.json"
    assert st.parentId == "cfg/team.json"  # = file_id → HAS_STATEMENT to the File
    assert st.startLine == 1
    assert "members[1]{name}:" in st.text  # the TOON body


def test_document_statement_captures_full_text_uncapped() -> None:
    # The parser keeps the full TOON — no clip, no ellipsis. Sizing (splitting a large
    # document into `#partNofN` records) happens once at emit (see test_split),
    # so the parser must not lose the tail here regardless of statement_text_limit.
    big = {"rows": [{"v": "y" * 100} for _ in range(50)]}
    rec = _parse("a.json", big, capture_statements=True, statement_text_limit=200)
    st = rec.statements[0]
    assert len(st.text) > 200 and not st.text.endswith("…")  # full document, not clipped
    assert st.text.count("y" * 100) == 50  # every row's value survives
    assert "toon" not in rec.metadata  # content is not duplicated on metadata


def test_record_serializes_schema_valid_with_statement() -> None:
    rec = _parse(
        "a.json", {"values": [{"id": "A", "nested": {"k": [1, 2]}}]}, capture_statements=True
    )
    line = rec.model_dump_json(by_alias=True, exclude_none=True)
    assert '"type":"config"' in line and '"structured-json"' in line
    assert '"structured_data"' in line


# ── single ownership of .json (registry integration) ────────────────────────────
def test_json_is_sole_owner_of_dot_json() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        for path in ("records.json", "package.json", "tsconfig.json", "empty.json", "cfg.json"):
            assert registry.select(path, b'{"a":1}').name == "json"
        assert registry.base_parser_for("records.json").name == "json"
        # selection never parses, so it is safe on adversarially nested JSON
        assert registry.select("x.json", ("[" * 1200 + "]" * 1200).encode()).name == "json"
    finally:
        registry.clear()
        registry.discover_builtin()
