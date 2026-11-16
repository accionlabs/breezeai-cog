"""``scripts/struts2_action_count.py``: the independent count agrees with cog on a repo built
from scratch, and reports a mismatch when cog would miss an action."""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "struts2_action_count.py"


def _script():
    spec = importlib.util.spec_from_file_location("struts2_action_count", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> Path:
    files = {
        "src/main/resources/struts.xml": (
            '<struts><package name="p" namespace="/s"><action name="a" class="x.A"/>'
            '<action name="b"/></package></struts>'),
        "src/main/resources/other.xml": "<beans><action name='not-struts'/></beans>",
        "src/main/java/x/actions/C.java": (
            "package x.actions;\nimport org.apache.struts2.convention.annotation.*;\n"
            "public class C {\n  // @Action(\"commented\")\n"
            '  @Actions({@Action("c1"), @Action("c2")}) public String m() { return ""; }\n'
            '  @Action("c3") public String n() { return "@Action"; }\n}\n'),
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_counts_agree(tmp_path) -> None:
    script = _script()
    repo = _repo(tmp_path)
    want = script.expected(repo)
    got, convention, _ = script.recorded(repo)
    assert want == {"src/main/resources/struts.xml": 2, "src/main/java/x/actions/C.java": 3}
    assert got == want and not convention


def test_reports_a_mismatch(tmp_path, monkeypatch, capsys) -> None:
    script = _script()
    repo = _repo(tmp_path)
    real = script.recorded

    def missing_one(r):  # as if cog lost one annotation
        declared, convention, rows = real(r)
        return declared - Counter({"src/main/java/x/actions/C.java": 1}), convention, rows

    monkeypatch.setattr(script, "recorded", missing_one)
    monkeypatch.setattr("sys.argv", ["struts2_action_count.py", str(repo)])
    assert script.main() == 1
    assert "C.java: expected 3, recorded 2" in capsys.readouterr().out
