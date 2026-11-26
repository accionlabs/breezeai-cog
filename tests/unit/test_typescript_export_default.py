"""``export default`` of a function or class written without a name (anonymous-functions
proposal, JS/TS): a Function / Class named ``default`` — what an importer of the default export
calls — the class ``isAnonymous``. Before, the file had no record of that code at all."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _parse(tmp_path: Path, src: str, name: str = "app.ts"):
    p = tmp_path / name
    p.write_text(src)
    ctx = ParseContext(path=name, abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=True)
    return TypeScriptParser().parse_file(ctx)


@pytest.mark.parametrize("src, kind", [
    ("export default () => render();\n", "arrow_function"),
    ("export default async function () { render(); }\n", "function_expression"),
])
def test_an_anonymous_default_function_is_named_default(tmp_path: Path, src: str, kind: str) -> None:
    rec = _parse(tmp_path, src)
    assert [(f.name, f.type, [c.name for c in f.calls]) for f in rec.functions] == [
        ("default", kind, ["render"])]
    assert rec.calls is None  # its code is the Function's, not the file's


def test_an_anonymous_default_class_is_named_default(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "export default class { run() { go(); } }\n", "app.js")
    [cls] = rec.classes
    assert (cls.name, cls.isAnonymous) == ("default", True)
    assert [(f.name, f.parentId) for f in rec.functions] == [("run", cls.id)]


def test_a_named_default_and_a_wrapped_value_are_unchanged(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "export default function named() { x(); }\n")
    assert [f.name for f in rec.functions] == ["named"]
    rec = _parse(tmp_path, "export default memo(() => draw());\n")
    assert rec.functions == [] and [c.name for c in rec.calls] == ["memo", "draw"]
