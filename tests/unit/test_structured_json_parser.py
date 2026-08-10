"""StructuredJsonParser: record-structure claim gating, full recursive key/value capture
onto File.metadata (no Function nodes), credential redaction, bounds, and selection
priority vs ConfigParser."""

from __future__ import annotations

import json

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.structured_json.parser import StructuredJsonParser


def _parse(path: str, obj, metadata_value_limit: int | None = None):
    src = json.dumps(obj, ensure_ascii=False).encode()
    kwargs = {} if metadata_value_limit is None else {"metadata_value_limit": metadata_value_limit}
    ctx = ParseContext(path=path, abs_path=None, source=src, repo_root=".", **kwargs)
    return StructuredJsonParser().parse_file(ctx)


def _fields(path: str, obj, metadata_value_limit: int | None = None) -> dict:
    return _parse(path, obj, metadata_value_limit).metadata["fields"]


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
    # dict-of-strings, not array-of-objects → left to ConfigParser
    assert _claims({"name": "app", "dependencies": {"react": "^18"}}) is False


def test_does_not_claim_array_of_scalars() -> None:
    assert _claims(["a", "b", "c"]) is False


def test_does_not_claim_malformed_json() -> None:
    assert StructuredJsonParser().claims("x.json", b"{not json") is False


def test_does_not_crash_on_deeply_nested_json() -> None:
    # A JSON array nested 1200 levels deep exceeds Python's default recursion limit
    # (1000) during decoding. Claims must return False, not propagate RecursionError.
    depth = 1200
    src = ("[" * depth + "]" * depth).encode()
    assert StructuredJsonParser().claims("x.json", src) is False


# ── denylist guard: don't steal ConfigParser's richly-handled JSON ──────────────
def test_does_not_claim_package_json_with_contributors_array() -> None:
    # `contributors: [{...}]` is a record array but package.json must stay with ConfigParser
    src = json.dumps({"name": "app", "dependencies": {"react": "^18"},
                      "contributors": [{"name": "Al", "email": "a@x.com"}]}).encode()
    assert StructuredJsonParser().claims("package.json", src) is False
    assert StructuredJsonParser().claims("pkgs/app/package.json", src) is False  # path, not just basename


def test_does_not_claim_tsconfig_with_references_array() -> None:
    src = json.dumps({"compilerOptions": {"strict": True},
                      "references": [{"path": "../lib"}]}).encode()
    assert StructuredJsonParser().claims("tsconfig.json", src) is False
    assert StructuredJsonParser().claims("jsconfig.json", src) is False


def test_still_claims_other_configs_with_record_arrays() -> None:
    # composer.json / openapi.json aren't richly handled by ConfigParser → capturing is a win
    assert StructuredJsonParser().claims(
        "composer.json", json.dumps({"name": "v/p", "authors": [{"name": "X"}]}).encode()
    ) is True


# ── emission: File.metadata only, no Function/Class nodes ───────────────────────
def test_emits_config_record_no_functions() -> None:
    rec = _parse("json/records.json", {"values": [{"code": "REC-1"}]})
    assert rec.type == "config" and rec.language == "structured-json"
    assert rec.functions == [] and rec.classes == []  # never mints graph-function noise
    assert rec.metadata["kind"] == "structured-json"


def test_full_recursive_flatten_of_whole_document() -> None:
    rec = _parse(
        "json/records.json",
        {"values": [{"code": "REC-1", "label": "widget",
                     "spec": {"2020": {"01": {"kind": "full"}}},
                     "note": None}]},
    )
    f = rec.metadata["fields"]
    # whole-document flatten: the wrapper array index is part of the path
    assert f["values[0].code"] == "REC-1"
    assert f["values[0].label"] == "widget"
    assert f["values[0].spec.2020.01.kind"] == "full"
    assert f["values[0].note"] == ""  # None emitted as explicit empty (present != absent)
    assert rec.metadata["leafCount"] == len(f)


def test_null_leaves_emitted_as_empty_not_dropped() -> None:
    # a present-but-null field is kept as "" so it is distinguishable from an absent one.
    present = _fields("a.json", [{"code": "REC", "rule": "x", "joins": None}])
    assert present["[0].joins"] == ""            # present-but-null -> explicit empty
    absent = _fields("a.json", [{"code": "REC", "rule": "x"}])
    assert "[0].joins" not in absent             # truly absent -> still absent
    # null elements inside a list are also emitted as empty
    lst = _fields("a.json", [{"vals": ["x", None, "y"]}])
    assert lst["[0].vals[1]"] == ""


def test_no_field_is_interpreted_as_a_name() -> None:
    # agnostic: an id-like field is captured as data, never promoted to a node name
    rec = _parse("a.json", [{"id": "ORD-1", "name": "Widget"}])
    assert rec.functions == []
    assert _fields("a.json", [{"id": "ORD-1"}])["[0].id"] == "ORD-1"


def test_credential_keys_redacted_including_nested() -> None:
    f = _fields("a.json", [{"user": "bob", "password": "hunter2", "creds": {"apiKey": "x"}}])
    assert f["[0].password"] == "***"
    assert f["[0].creds.apiKey"] == "***"
    assert f["[0].user"] == "bob"


def test_value_length_kept_under_default_limit() -> None:
    # A multi-KB leaf is preserved up to the default cap (4000 in ParseContext), not the
    # older 500 fallback — so realistic large metadata values are no longer over-truncated.
    long = "x" * 5000
    f = _fields("a.json", [{"blob": long}])
    assert f["[0].blob"] == long[:4000] + "…"  # capped at the 4000 default, not 500
    assert len(f["[0].blob"]) == 4001


def test_value_length_bound_is_configurable() -> None:
    long = "x" * 5000
    f = _fields("a.json", [{"blob": long}], metadata_value_limit=500)
    assert f["[0].blob"].endswith("…")
    assert len(f["[0].blob"]) == 501


def test_value_length_truncation_disabled_at_zero() -> None:
    long = "x" * 5000
    f = _fields("a.json", [{"blob": long}], metadata_value_limit=0)
    assert f["[0].blob"] == long  # 0 disables truncation


def test_leaf_count_bound_and_flag() -> None:
    big = [{"n": i} for i in range(StructuredJsonParser.MAX_LEAVES + 100)]
    rec = _parse("a.json", big)
    assert rec.metadata["leafCount"] == StructuredJsonParser.MAX_LEAVES
    assert rec.metadata["truncated"] is True


def test_record_serializes_schema_valid() -> None:
    rec = _parse("a.json", {"values": [{"id": "A", "nested": {"k": [1, 2]}}]})
    line = rec.model_dump_json(by_alias=True, exclude_none=True)
    assert '"type":"config"' in line and '"structured-json"' in line


# ── selection priority (registry integration) ──────────────────────────────────
def test_selection_beats_config_for_records_only() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("records.json", b'{"values":[{"a":1}]}').name == "structured-json"
        assert registry.select("package.json", b'{"name":"x"}').name == "config"
        assert registry.select("batch_config.json", b'{"prefix":"http://"}').name == "config"
        # denylist: record-array inside a specialized config still routes to ConfigParser
        assert registry.select(
            "package.json", b'{"name":"x","contributors":[{"name":"a"}]}'
        ).name == "config"
        assert registry.select(
            "tsconfig.json", b'{"references":[{"path":"../x"}]}'
        ).name == "config"
        # base language label / extension allow-list still driven by ConfigParser (priority 0)
        assert registry.base_parser_for("records.json").name == "config"
    finally:
        registry.clear()
        registry.discover_builtin()
