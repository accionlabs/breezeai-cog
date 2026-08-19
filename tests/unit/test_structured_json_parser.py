"""StructuredJsonParser: record-structure claim gating, whole-document TOON capture onto
File.metadata (no Function nodes), the gated single `structured_data` statement, credential
redaction, bounds, and selection priority vs ConfigParser."""

from __future__ import annotations

import json

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.structured_json.parser import StructuredJsonParser


def _parse(
    path: str,
    obj,
    *,
    metadata_value_limit: int | None = None,
    capture_statements: bool = False,
    statement_text_limit: int = 8000,
):
    src = json.dumps(obj, ensure_ascii=False).encode()
    kwargs = {} if metadata_value_limit is None else {"metadata_value_limit": metadata_value_limit}
    ctx = ParseContext(
        path=path,
        abs_path=None,
        source=src,
        repo_root=".",
        capture_statements=capture_statements,
        statement_text_limit=statement_text_limit,
        **kwargs,
    )
    return StructuredJsonParser().parse_file(ctx)


def _toon(path: str, obj, **kw) -> str:
    # TOON lives on the (gated) statement, not on metadata — capture it and read its text
    return _parse(path, obj, capture_statements=True, **kw).statements[0].text


def _claims(obj) -> bool:
    return StructuredJsonParser().claims("x.json", json.dumps(obj).encode())


# ── claim gating (which JSON is captured in full) ──────────────────────────────
def test_claims_values_array_of_records() -> None:
    assert _claims({"values": [{"code": "REC-1"}]}) is True


def test_claims_root_array_of_objects() -> None:
    assert _claims([{"id": 1}, {"id": 2}]) is True


def test_does_not_claim_flat_config() -> None:
    assert _claims({"prefix": "http://"}) is False


def test_does_not_claim_package_json_shape() -> None:
    assert _claims({"name": "app", "dependencies": {"react": "^18"}}) is False


def test_does_not_claim_array_of_scalars() -> None:
    assert _claims(["a", "b", "c"]) is False


def test_does_not_claim_malformed_json() -> None:
    assert StructuredJsonParser().claims("x.json", b"{not json") is False


def test_does_not_crash_on_deeply_nested_json() -> None:
    depth = 1200
    src = ("[" * depth + "]" * depth).encode()
    assert StructuredJsonParser().claims("x.json", src) is False


# ── denylist guard: don't steal ConfigParser's richly-handled JSON ──────────────
def test_does_not_claim_package_json_with_contributors_array() -> None:
    src = json.dumps(
        {
            "name": "app",
            "dependencies": {"react": "^18"},
            "contributors": [{"name": "Al", "email": "a@x.com"}],
        }
    ).encode()
    assert StructuredJsonParser().claims("package.json", src) is False
    assert StructuredJsonParser().claims("pkgs/app/package.json", src) is False


def test_does_not_claim_tsconfig_with_references_array() -> None:
    src = json.dumps(
        {"compilerOptions": {"strict": True}, "references": [{"path": "../lib"}]}
    ).encode()
    assert StructuredJsonParser().claims("tsconfig.json", src) is False
    assert StructuredJsonParser().claims("jsconfig.json", src) is False


def test_still_claims_other_configs_with_record_arrays() -> None:
    assert (
        StructuredJsonParser().claims(
            "composer.json", json.dumps({"name": "v/p", "authors": [{"name": "X"}]}).encode()
        )
        is True
    )


# ── emission: File.metadata (TOON), no Function/Class nodes ─────────────────────
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


def test_value_length_capped_at_default() -> None:
    long = "x" * 5000
    toon = _toon("a.json", [{"blob": long}])
    assert "x" * 4000 + "…" in toon
    assert "x" * 4001 not in toon  # capped at the 4000 default


def test_value_length_bound_is_configurable() -> None:
    long = "x" * 5000
    toon = _toon("a.json", [{"blob": long}], metadata_value_limit=500)
    assert "x" * 500 + "…" in toon
    assert "x" * 501 not in toon


def test_value_length_truncation_disabled_at_zero() -> None:
    long = "x" * 5000
    toon = _toon("a.json", [{"blob": long}], metadata_value_limit=0)
    assert long in toon


def test_leaf_count_bound_and_flag() -> None:
    big = [{"n": i} for i in range(StructuredJsonParser.MAX_LEAVES + 100)]
    rec = _parse("a.json", big)
    assert rec.metadata["leafCount"] == StructuredJsonParser.MAX_LEAVES
    assert rec.metadata["truncated"] is True


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
    # document into `#partNofN` records) happens once at emit (see test_pipeline_split),
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


# ── selection priority (registry integration) ──────────────────────────────────
def test_selection_beats_config_for_records_only() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("records.json", b'{"values":[{"a":1}]}').name == "structured-json"
        assert registry.select("package.json", b'{"name":"x"}').name == "config"
        assert registry.select("batch_config.json", b'{"prefix":"http://"}').name == "config"
        assert (
            registry.select("package.json", b'{"name":"x","contributors":[{"name":"a"}]}').name
            == "config"
        )
        assert (
            registry.select("tsconfig.json", b'{"references":[{"path":"../x"}]}').name == "config"
        )
        assert registry.base_parser_for("records.json").name == "config"
    finally:
        registry.clear()
        registry.discover_builtin()
