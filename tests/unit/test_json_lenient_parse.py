"""JSON that its own reader accepts but strict JSON rejects: a raw tab or line break inside a
string, and ``//`` / ``/* */`` comments. These files are captured like any other JSON, with
``metadata.parseMode`` naming the fallback; broken JSON still records the strict error."""

from __future__ import annotations

import json

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.config.extractors import extract_config, load_json
from breezeai_cog.parsers.structured_json.parser import JsonParser


def _parse(path: str, text: str):
    ctx = ParseContext(
        path=path,
        abs_path=None,
        source=text.encode(),
        repo_root=".",
        capture_statements=True,
        statement_text_limit=8000,
    )
    return JsonParser().parse_file(ctx)


# ── load_json ────────────────────────────────────────────────────────────────
def test_strict_json_has_no_mode() -> None:
    assert load_json('{"a": 1}') == ({"a": 1}, None)


def test_raw_newline_in_string_is_lenient() -> None:
    data, mode = load_json('{"MWRTD02": "first\nsecond"}')
    assert mode == "lenient"
    assert data == {"MWRTD02": "first\nsecond"}  # kept as written


def test_raw_tab_in_string_is_lenient() -> None:
    data, mode = load_json('{"params": "\t_ _ 5500 1"}')
    assert mode == "lenient"
    assert data["params"] == "\t_ _ 5500 1"


def test_line_comment_is_jsonc() -> None:
    data, mode = load_json('// vert.x stack descriptor\n{"version": "4.5"}')
    assert (data, mode) == ({"version": "4.5"}, "jsonc")


def test_block_comment_is_jsonc() -> None:
    data, mode = load_json('{\n  /* the stack */\n  "a": 1 /* trailing */\n}')
    assert (data, mode) == ({"a": 1}, "jsonc")


def test_comment_and_raw_tab_together() -> None:
    data, mode = load_json('// header\n{"p": "\tx"}')
    assert (data, mode) == ({"p": "\tx"}, "jsonc")


def test_slashes_inside_a_string_are_not_a_comment() -> None:
    # strict JSON: the URL is never touched
    assert load_json('{"url": "http://host/a//b"}') == ({"url": "http://host/a//b"}, None)
    # JSONC: a real comment is removed, the URL in a value is kept
    data, mode = load_json('{"url": "http://host/x" // the api\n}')
    assert (data, mode) == ({"url": "http://host/x"}, "jsonc")


def test_broken_json_raises_the_strict_error() -> None:
    with pytest.raises(json.JSONDecodeError) as caught:
        load_json('{"a": 1')
    strict = None
    try:
        json.loads('{"a": 1')
    except json.JSONDecodeError as exc:
        strict = str(exc)
    assert str(caught.value) == strict


def test_broken_json_with_a_comment_reports_the_strict_error() -> None:
    # the comment-stripped retry also fails; the message is the strict parser's, at line 1
    with pytest.raises(json.JSONDecodeError) as caught:
        load_json('// c\n{"a": ')
    assert caught.value.lineno == 1 and caught.value.colno == 1


# ── JsonParser: captured like any other JSON ─────────────────────────────────
@pytest.mark.parametrize(
    "text, mode",
    [
        ('{"MWRTD02": "CDRPに差分レポート情報を\n登録する"}', "lenient"),
        ('{"jobs": [{"params": "\t_ _ 550000000026 1"}]}', "lenient"),
        ('// vert.x stack descriptor\n{"version": "4.5.1", "dependencies": []}', "jsonc"),
        ('{\n  /* block */\n  "a": "http://x//y"\n}', "jsonc"),
    ],
)
def test_lenient_files_are_captured(text: str, mode: str) -> None:
    rec = _parse("cfg/data.json", text)
    assert rec.language == "structured-json"
    assert rec.metadata["parseMode"] == mode
    assert "parseError" not in rec.metadata
    assert [s.semanticType for s in rec.statements] == ["structured_data"]


def test_raw_tab_value_is_kept_as_read() -> None:
    # the value is not rewritten: a raw tab gives the same capture as a strictly escaped one
    raw = _parse("cfg/batch_config.json", '{"params": "\tA B"}')
    escaped = _parse("cfg/batch_config.json", '{"params": "\\tA B"}')
    assert raw.statements[0].text == escaped.statements[0].text


def test_strict_json_output_is_unchanged() -> None:
    rec = _parse("cfg/data.json", '{"a": "1", "b": [1, 2]}')
    assert "parseMode" not in rec.metadata
    assert rec.metadata == {
        "kind": "structured-json",
        "category": "json",
        "format": "toon",
        "topLevelKeys": ["a", "b"],
        "recordCount": 0,
        "leafCount": rec.metadata["leafCount"],
    }


def test_broken_json_keeps_parse_error() -> None:
    rec = _parse("cfg/data.json", '{"a": [1, 2}')
    assert rec.language == "config"
    assert rec.statements == []
    assert rec.metadata["parseError"].startswith("Expecting")
    assert "parseMode" not in rec.metadata


def test_broken_json_reports_the_strict_message() -> None:
    # a raw control character AND an unbalanced brace: the message is the strict one
    rec = _parse("cfg/data.json", '{"a": "x\ty"')
    assert rec.metadata["parseError"].startswith("Invalid control character")


# ── named configs read through the same loader ───────────────────────────────
def test_tsconfig_with_comments_is_read() -> None:
    meta = extract_config(
        "tsconfig.json", '{\n  // paths\n  "compilerOptions": {"baseUrl": "src"}\n}'
    )
    assert meta["compilerConfig"]["baseUrl"] == "src"
    assert meta["parseMode"] == "jsonc"
    assert "parseError" not in meta


def test_strict_named_config_has_no_mode() -> None:
    meta = extract_config("package.json", '{"name": "app"}')
    assert "parseMode" not in meta and meta["packageInfo"]["name"] == "app"


def test_mod_json_comments_still_read() -> None:
    meta = extract_config("mod.json", '// module\n{"main": "groovy:com.acme.Main"}')
    assert meta["verticleMain"] == "com.acme.Main"
    assert meta["parseMode"] == "jsonc"
