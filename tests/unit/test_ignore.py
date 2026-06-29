"""Tests for the global ignore/include engine (hierarchical repo-tree matching is
covered in test_scanner)."""

from __future__ import annotations

from breezeai_cog.core.ignore import IgnoreEngine, builtin_ignore_lines


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
