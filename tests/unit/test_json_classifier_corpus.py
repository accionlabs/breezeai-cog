"""Locked spec for JSON classification.

``JsonParser`` is the single owner of ``.json``; each row asserts the *outcome* it produces
for a given shape — either a full **capture** (``language="structured-json"``, TOON on a
``structured_data`` statement) or a **config** record (``language="config"``, a reduced/rich
extractor summary). This is the acceptance test and a regression guard.

Outcome rule:

    capture(path, data)  when  data is a non-empty (dict | list)
                         AND   Path(path).name not in RICH_JSON_NAMES
    else config          (named-rich extractor, or empty/scalar with nothing to capture)
"""

from __future__ import annotations

import json

import pytest

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext

#: JSON filenames the shared extractor handles richly — captured in full otherwise.
NAME_RICH = {"package.json", "tsconfig.json", "jsconfig.json", "mod.json"}

CONFIG = "config"
CAPTURE = "structured-json"

# (path, json_obj, expected record language)
_CASES = [
    # ── named-rich configs: rich extractor, language "config" ───────────────────
    pytest.param("package.json", {"name": "app", "dependencies": {"react": "^18"}}, CONFIG,
                 id="named:package.json"),
    pytest.param("tsconfig.json", {"compilerOptions": {"strict": True}}, CONFIG,
                 id="named:tsconfig.json"),
    pytest.param("jsconfig.json", {"compilerOptions": {"baseUrl": "."}}, CONFIG,
                 id="named:jsconfig.json"),
    pytest.param("mod.json", {"main": "Verticle", "requires": {}}, CONFIG, id="named:mod.json"),

    # ── data (record arrays) ────────────────────────────────────────────────────
    pytest.param("n8n-workflow.json", {"nodes": [{"id": "1"}], "connections": {}}, CAPTURE,
                 id="data:n8n-nodes-array"),
    pytest.param("json/MAAP(X).json", {"values": [{"MAAPAK1": "MAAP"}]}, CAPTURE,
                 id="data:maap-values-array"),
    pytest.param("records.json", [{"id": 1}, {"id": 2}], CAPTURE, id="data:root-record-array"),

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

    # ── heterogeneous configs w/o rich extractor: captured too ──────────────────
    pytest.param(".eslintrc.json", {"env": {"node": True}, "rules": {"eqeqeq": "error"},
                                    "extends": ["airbnb"]}, CAPTURE, id="config:eslintrc-heterogeneous"),
    pytest.param("angular.json", {"version": 1, "projects": {"app": {"root": ""}}}, CAPTURE,
                 id="config:angular.json"),
    pytest.param("tsconfig.base.json", {"compilerOptions": {"strict": True}}, CAPTURE,
                 id="config:tsconfig.base-non-exact-name"),

    # ── nothing to capture: config record ───────────────────────────────────────
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


def _record(path: str, obj):
    src = json.dumps(obj).encode()
    parser = registry.select(path, src)
    assert parser is not None and parser.name == "json"  # single .json owner
    ctx = ParseContext(path=path, abs_path=None, source=src, repo_root=".",
                       capture_statements=True, statement_text_limit=8000)
    return parser.parse_file(ctx)


@pytest.mark.parametrize("path,obj,target", _CASES)
def test_classifier_outcome(path: str, obj, target: str) -> None:
    assert _record(path, obj).language == target


# ── invariants ──────────────────────────────────────────────────────────────────
def test_named_rich_never_captured_even_with_record_array() -> None:
    # a stray array-of-objects (package.json contributors, tsconfig references) must NOT
    # divert a named-rich config into full capture
    assert _record("package.json", {"name": "app", "contributors": [{"name": "Al"}]}).language == CONFIG
    assert _record("tsconfig.json", {"compilerOptions": {}, "references": [{"path": "../lib"}]}).language == CONFIG


def test_captured_data_preserves_values_not_just_keys() -> None:
    # once captured, the lookup table's VALUES survive — the whole point of the refactor
    rec = _record("cms.json", {"00 01 03": "template1", "00 01 05": "template2"})
    assert rec.language == CAPTURE
    toon = rec.statements[0].text
    assert "template1" in toon and "template2" in toon
