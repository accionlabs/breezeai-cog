"""SkipReport tests: reason tally, extension histogram, dir handling, serialization."""

from __future__ import annotations

from breezeai_cog.core.skips import SkipReport


def test_files_tallied_by_reason() -> None:
    r = SkipReport()
    r.record("a.md", "unsupported")
    r.record("b.md", "unsupported")
    r.record("c.txt", "unsupported")
    r.record("secret.env", "ignored")
    r.record("huge.bin", "oversized", size=5_000_000)

    assert r.counts == {"unsupported": 3, "ignored": 1, "oversized": 1}
    assert r.total_files == 5
    assert not r.is_empty


def test_unsupported_extension_histogram() -> None:
    r = SkipReport()
    for p in ("a.md", "b.md", "c.png", "Makefile"):
        r.record(p, "unsupported")
    r.record("x.py", "ignored")  # ignored files don't feed the extension histogram

    assert r.extensions == {".md": 2, ".png": 1, "<none>": 1}
    assert r.top_extensions(2) == [(".md", 2), (".png", 1)]


def test_directories_not_counted_as_files() -> None:
    r = SkipReport()
    r.record("node_modules", "ignored", is_dir=True)
    r.record("dist", "ignored", is_dir=True)
    r.record("a.md", "unsupported")

    assert r.dirs == ["node_modules", "dist"]
    assert r.counts == {"unsupported": 1}  # dirs excluded from the file-level tally
    assert r.total_files == 1


def test_oversized_records_size() -> None:
    r = SkipReport()
    r.record("big.js", "oversized", size=3_145_728)
    assert r.files == [{"path": "big.js", "reason": "oversized", "size": 3_145_728}]


def test_empty_report() -> None:
    assert SkipReport().is_empty


def test_to_dict_shape() -> None:
    r = SkipReport()
    r.record("a.md", "unsupported")
    r.record("node_modules", "ignored", is_dir=True)
    r.record("big.bin", "oversized", size=9_000_000)

    d = r.to_dict()
    assert d["summary"] == {"unsupported": 1, "oversized": 1}
    assert d["totalSkippedFiles"] == 2
    assert d["unsupportedExtensions"] == {".md": 1}
    assert d["ignoredDirectories"] == ["node_modules"]
    assert {"path": "big.bin", "reason": "oversized", "size": 9_000_000} in d["files"]
