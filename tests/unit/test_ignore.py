"""Tests for the global ignore/include engine (hierarchical repo-tree matching is
covered in test_scanner)."""

from __future__ import annotations

from breezeai_cog.core.ignore import IgnoreEngine, builtin_ignore_lines
from breezeai_cog.core.registry import discover_builtin, registered


def test_builtin_ignores_common_dirs_and_tests() -> None:
    eng = IgnoreEngine.build()
    for path in [
        "node_modules/x.js",
        ".git/config",
        "src/__pycache__/a.pyc",
        "dist/bundle.js",
        ".env",
        "tests/test_c.py",
        "src/test_helper.py",  # test_*.py at any depth
        "app.min.js",
    ]:
        assert eng.is_ignored_global(path), path


def test_builtin_keeps_normal_sources() -> None:
    eng = IgnoreEngine.build()
    for path in ["a.py", "src/order.py", "pkg/main.go"]:
        assert not eng.is_ignored_global(path), path


def test_directory_form_matches() -> None:
    eng = IgnoreEngine.build()
    assert eng.is_ignored_global("node_modules/")
    assert eng.is_ignored_global("a/b/__pycache__/")


def test_include_overrides_are_separate_set() -> None:
    eng = IgnoreEngine(builtin_ignore_lines(), ["src/test_helper.py"])
    assert eng.is_ignored_global("src/test_helper.py")
    assert eng.is_included_global("src/test_helper.py")
    assert not eng.is_included_global("src/other.py")


def test_per_language_ignore_is_scoped_not_global() -> None:
    """Regression for G1: C#'s NuGet ``packages/`` must not prune a TS ``packages/``
    workspace. Layer-2 patterns are scoped to the file's own language, not global."""
    discover_builtin()
    eng = IgnoreEngine.build(registered())

    # `packages/` is NOT a walk-time (global/universal) ignore anymore.
    assert not eng.is_ignored_global("packages/graphql-server/src/server.ts")

    # It applies only to C#/VB files (its owning languages) …
    assert eng.is_lang_ignored("src/packages/Foo.cs", "csharp")
    assert eng.is_lang_ignored("src/packages/Foo.vb", "vb")
    # … and never to a TypeScript file under a `packages/` workspace dir.
    assert not eng.is_lang_ignored("packages/graphql-server/src/server.ts", "typescript")

    # A language's own layer-2 patterns still fire (not a blanket disable).
    assert eng.is_lang_ignored("src/app.d.ts", "typescript")   # typescript ignore.txt
    assert eng.is_lang_ignored("pkg/__pycache__/m.py", "python")
