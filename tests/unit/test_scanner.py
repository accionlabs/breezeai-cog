"""Scanner tests: filter chain, directory pruning, hierarchical .repoignore,
.repoinclude override, max_file_size, and symlinked-dir safety."""

from __future__ import annotations

import os

from breezeai_cog.core.ignore import IgnoreEngine
from breezeai_cog.core.scanner import scan


def classify(path: str) -> str | None:
    return "python" if path.endswith(".py") else None


def _build_repo(root) -> None:
    (root / "a.py").write_text("x = 1\n")
    (root / "src").mkdir()
    (root / "src" / "b.py").write_text("y = 2\n")
    (root / "src" / "test_helper.py").write_text("z = 3\n")  # ignored by test_*.py
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.py").write_text("nope\n")  # pruned dir
    (root / "sub").mkdir()
    (root / "sub" / ".repoignore").write_text("*.py\n")  # hierarchical ignore
    (root / "sub" / "d.py").write_text("d = 4\n")
    (root / "sub" / "e.txt").write_text("not python\n")  # not a known extension
    (root / "big.py").write_text("# " + "A" * 2000 + "\n")  # over max_file_size
    (root / ".repoinclude").write_text("src/test_helper.py\n")  # re-include


def test_scan_filter_chain(tmp_path) -> None:
    _build_repo(tmp_path)
    try:
        os.symlink(tmp_path, tmp_path / "link", target_is_directory=True)  # loop bait
    except (OSError, NotImplementedError):
        pass

    skips: list[tuple[str, str, bool, int | None]] = []

    def _on_skip(p: str, r: str, *, is_dir: bool = False, size: int | None = None) -> None:
        skips.append((p, r, is_dir, size))

    entries = list(
        scan(tmp_path, classify, engine=IgnoreEngine.build(), max_file_size=1000,
             on_skip=_on_skip)
    )
    found = sorted(e.path for e in entries)

    assert found == ["a.py", "src/b.py", "src/test_helper.py"]
    assert all(e.language == "python" for e in entries)
    # negative cases
    assert "node_modules/x.py" not in found  # dir pruned
    assert "sub/d.py" not in found  # hierarchical .repoignore
    assert "big.py" not in found  # size filter
    reasons = {(p, r) for p, r, _is_dir, _size in skips}
    assert ("big.py", "oversized") in reasons
    assert ("sub/d.py", "ignored") in reasons  # dropped by hierarchical .repoignore
    assert ("sub/e.txt", "unsupported") in reasons  # no parser for the extension
    # pruned directory reported once with is_dir=True (not per contained file)
    assert ("node_modules", "ignored", True) in {(p, r, d) for p, r, d, _ in skips}
    # oversized carries the byte size
    assert any(p == "big.py" and r == "oversized" and s and s > 1000 for p, r, _d, s in skips)
    # symlinked dir never recursed (no duplicate / link-prefixed paths)
    assert not any(p.startswith("link/") for p in found)


def _classify_any(path: str) -> str | None:
    """Claims every extension used in the template tests, so a dropped file is provably
    dropped by ``skip_rule`` and not by the extension allow-list."""
    return "lang" if "." in path.rsplit("/", 1)[-1] else None


def _template_rule(path: str) -> str | None:
    return "template" if path.lower().endswith((".html", ".aspx", ".vue")) else None


def _build_markup_repo(root) -> None:
    (root / "app.html").write_text("<div></div>\n")
    (root / "Page.aspx").write_text("<%@ Page %>\n")
    (root / "Page.aspx.cs").write_text("class P {}\n")  # code-behind — must survive
    (root / "Widget.vue").write_text("<template><b/></template>\n")
    (root / "Widget.ts").write_text("export const x = 1\n")


def test_skip_rule_drops_templates_but_keeps_code_behind(tmp_path) -> None:
    _build_markup_repo(tmp_path)
    skips: list[tuple[str, str]] = []

    entries = list(
        scan(tmp_path, _classify_any, engine=IgnoreEngine.build(), max_file_size=10_000,
             skip_rule=_template_rule,
             on_skip=lambda p, r, **kw: skips.append((p, r)))
    )

    # `.aspx.cs` / `.ts` are code, not markup — matching on the final suffix keeps them.
    assert sorted(e.path for e in entries) == ["Page.aspx.cs", "Widget.ts"]
    assert set(skips) == {("app.html", "template"), ("Page.aspx", "template"),
                          ("Widget.vue", "template")}


def test_no_skip_rule_keeps_templates(tmp_path) -> None:
    _build_markup_repo(tmp_path)
    entries = list(
        scan(tmp_path, _classify_any, engine=IgnoreEngine.build(), max_file_size=10_000)
    )
    assert len(entries) == 5  # every file survives when the gate is off


def test_ignored_template_reports_ignored_not_template(tmp_path) -> None:
    """The gate runs *after* ignore/include, so an ignored template keeps the `ignored`
    bucket — the template count never absorbs ordinary ignore noise."""
    (tmp_path / ".repoignore").write_text("skipme/\n")
    (tmp_path / "skipme").mkdir()
    (tmp_path / "skipme" / "x.html").write_text("<i/>\n")
    (tmp_path / "keep.html").write_text("<i/>\n")
    skips: list[tuple[str, str, bool]] = []

    list(scan(tmp_path, _classify_any, engine=IgnoreEngine.build(), max_file_size=10_000,
              skip_rule=_template_rule,
              on_skip=lambda p, r, **kw: skips.append((p, r, kw.get("is_dir", False)))))

    assert ("skipme", "ignored", True) in skips  # pruned as a directory, never reached
    assert ("keep.html", "template", False) in skips


def test_repoinclude_does_not_override_the_template_gate(tmp_path) -> None:
    """`.repoinclude` overrides the *ignore* layers only. The template gate is a separate
    capture-scope axis — only --capture-templates lifts it."""
    (tmp_path / ".repoinclude").write_text("*.html\n")
    (tmp_path / "page.html").write_text("<i/>\n")
    skips: list[tuple[str, str]] = []

    entries = list(
        scan(tmp_path, _classify_any, engine=IgnoreEngine.build(), max_file_size=10_000,
             skip_rule=_template_rule, on_skip=lambda p, r, **kw: skips.append((p, r)))
    )
    assert "page.html" not in [e.path for e in entries]
    assert ("page.html", "template") in skips
