"""Locked spec for the JSON classifier.

Each row asserts which parser *selects* a given JSON shape — the single decision that
determines full capture (``structured-json``) vs rich/reduced config handling
(``config``). This is the classifier's acceptance test and a regression guard.

Target claim predicate (decision: capture-everything-unknown):

    claims(path, data) = data is a non-empty (dict | list)
                         AND Path(path).name not in NAME_RICH

``NAME_RICH`` are the JSON files ConfigParser extracts *richly* by name; every other
non-empty JSON is captured in full. Empty containers and scalar roots have nothing to
capture and stay with ConfigParser.
"""

from __future__ import annotations

import json

import pytest

from breezeai_cog.core import registry

#: JSON filenames ConfigParser owns with a dedicated rich extractor — never full-captured.
#: Must stay in sync with StructuredJsonParser.CONFIG_JSON_NAMES + ConfigParser._dispatch.
NAME_RICH = {"package.json", "tsconfig.json", "jsconfig.json", "mod.json"}

CONFIG = "config"
CAPTURE = "structured-json"

# (path, json_obj, target_parser)
_CASES = [
    # ── named-rich configs: stay with ConfigParser ──────────────────────────────
    pytest.param("package.json", {"name": "app", "dependencies": {"react": "^18"}}, CONFIG,
                 id="named:package.json"),
    pytest.param("tsconfig.json", {"compilerOptions": {"strict": True}}, CONFIG,
                 id="named:tsconfig.json"),
    pytest.param("jsconfig.json", {"compilerOptions": {"baseUrl": "."}}, CONFIG,
                 id="named:jsconfig.json"),
    pytest.param("mod.json", {"main": "Verticle", "requires": {}}, CONFIG,
                 id="named:mod.json"),

    # ── data (record arrays) ────────────────────────────────────────────────────
    pytest.param("n8n-workflow.json", {"nodes": [{"id": "1"}], "connections": {}}, CAPTURE,
                 id="data:n8n-nodes-array"),
    pytest.param("json/MAAP(X).json", {"values": [{"MAAPAK1": "MAAP"}]}, CAPTURE,
                 id="data:maap-values-array"),
    pytest.param("records.json", [{"id": 1}, {"id": 2}], CAPTURE,
                 id="data:root-record-array"),

    # ── map-shaped data: previously reduced to keys (values lost), now captured ──
    pytest.param("p3-keyed-map.json", {"19000101": {"MAAPP01": "x"}, "19000102": {"MAAPP01": "y"}},
                 CAPTURE, id="data:keyed-map-of-objects"),
    pytest.param("cms-template-mappings.json", {"00 01 03": "t1", "00 01 05": "t1"}, CAPTURE,
                 id="data:flat-scalar-map"),
    pytest.param("i18n/en-GB.json", {"permissionLevels": {"Excluded": "No Access"}}, CAPTURE,
                 id="data:nested-object-tree"),
    pytest.param("content-set-config-schema.json", {"tags": ["a", "b"], "cats": ["c"]}, CAPTURE,
                 id="data:dict-of-scalar-arrays"),
    pytest.param("allowlist.json", ["en", "fr", "de"], CAPTURE, id="data:root-scalar-array"),

    # ── heterogeneous configs w/o rich extractor: captured too (merge decision) ──
    pytest.param(".eslintrc.json", {"env": {"node": True}, "rules": {"eqeqeq": "error"},
                                    "extends": ["airbnb"]}, CAPTURE, id="config:eslintrc-heterogeneous"),
    pytest.param("angular.json", {"version": 1, "projects": {"app": {"root": ""}}}, CAPTURE,
                 id="config:angular.json"),
    pytest.param("tsconfig.base.json", {"compilerOptions": {"strict": True}}, CAPTURE,
                 id="config:tsconfig.base-non-exact-name"),

    # ── nothing to capture: stay with ConfigParser ──────────────────────────────
    pytest.param("empty.json", {}, CONFIG, id="empty:object"),
    pytest.param("empty-array.json", [], CONFIG, id="empty:array"),
    pytest.param("scalar.json", "hello", CONFIG, id="empty:scalar-root"),
]


@pytest.fixture(scope="module", autouse=True)
def _registry():
    registry.clear()
    registry.discover_builtin()
    yield
    registry.clear()
    registry.discover_builtin()


@pytest.mark.parametrize("path,obj,target", _CASES)
def test_classifier_selection(path: str, obj, target: str) -> None:
    parser = registry.select(path, json.dumps(obj).encode())
    assert parser is not None
    assert parser.name == target


# ── invariants the predicate must preserve (not shape-specific) ─────────────────
def test_named_rich_never_captured_even_with_record_array() -> None:
    # a stray array-of-objects (package.json contributors, tsconfig references) must NOT
    # divert a named-rich config into full capture
    pkg = {"name": "app", "contributors": [{"name": "Al"}]}
    ts = {"compilerOptions": {"strict": True}, "references": [{"path": "../lib"}]}
    assert registry.select("package.json", json.dumps(pkg).encode()).name == CONFIG
    assert registry.select("tsconfig.json", json.dumps(ts).encode()).name == CONFIG


def test_captured_data_preserves_values_not_just_keys() -> None:
    # once captured, the lookup table's VALUES survive — the whole point of the refactor
    from breezeai_cog.parsers.base import ParseContext
    from breezeai_cog.parsers.structured_json.parser import StructuredJsonParser

    obj = {"00 01 03": "template1", "00 01 05": "template2"}
    src = json.dumps(obj).encode()
    assert StructuredJsonParser().claims("m.json", src)
    ctx = ParseContext(path="m.json", abs_path=None, source=src, repo_root=".",
                       capture_statements=True, statement_text_limit=8000)
    toon = StructuredJsonParser().parse_file(ctx).statements[0].text
    assert "template1" in toon and "template2" in toon
