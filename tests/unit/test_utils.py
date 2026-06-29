"""Tests for utils: loc, text truncation/snippet, repo-relative paths, source cache."""

from __future__ import annotations

from breezeai_cog.utils import SourceCache, count_loc, repo_relative, snippet, truncate


def test_count_loc_excludes_blank_lines() -> None:
    assert count_loc("a\n\n  \nb\n") == 2
    assert count_loc("") == 0


def test_truncate() -> None:
    assert truncate("hello", 1000) == "hello"
    assert truncate("hello", 3) == "hel"
    assert truncate("hello", 0) == "hello"  # disabled


def test_snippet_is_1_based_inclusive() -> None:
    src = "L1\nL2\nL3\nL4"
    assert snippet(src, 2, 3) == "L2\nL3"
    assert snippet(src, 1, 1) == "L1"


def test_repo_relative_posix() -> None:
    assert repo_relative("/repo/src/a.py", "/repo") == "src/a.py"
    assert repo_relative("/repo/a.py", "/repo") == "a.py"


def test_source_cache_reads_once(tmp_path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("v1", encoding="utf-8")
    cache = SourceCache()
    assert cache.read_text(f) == "v1"
    f.write_text("v2", encoding="utf-8")  # changed on disk...
    assert cache.read_text(f) == "v1"  # ...but cached value is returned
    cache.clear()
    assert cache.read_text(f) == "v2"
