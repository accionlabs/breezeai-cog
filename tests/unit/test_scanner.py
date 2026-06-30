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

    skips: list[tuple[str, str]] = []
    entries = list(
        scan(tmp_path, classify, engine=IgnoreEngine.build(), max_file_size=1000,
             on_skip=lambda p, r: skips.append((p, r)))
    )
    found = sorted(e.path for e in entries)

    assert found == ["a.py", "src/b.py", "src/test_helper.py"]
    assert all(e.language == "python" for e in entries)
    # negative cases
    assert "node_modules/x.py" not in found  # dir pruned
    assert "sub/d.py" not in found  # hierarchical .repoignore
    assert "big.py" not in found  # size filter
    reasons = {(p, r) for p, r in skips}
    assert ("big.py", "oversized") in reasons
    assert ("sub/d.py", "ignored") in reasons  # dropped by hierarchical .repoignore
    # symlinked dir never recursed (no duplicate / link-prefixed paths)
    assert not any(p.startswith("link/") for p in found)
