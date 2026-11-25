"""``calls``: one entry per name and target file, in every language (Fix 5). Two calls with the
same name that go to different files are two entries, so each gets its CALLS edge; a call that
resolves to no file is kept once per name, beside those that do."""

from __future__ import annotations

from breezeai_cog import analyze_repo


def _calls(tmp_path, files: dict[str, str], target: str, fn: str) -> list[tuple]:
    import gzip
    import json

    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    result = analyze_repo(repo, out=tmp_path / "out", jobs=1)
    for line in gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()[1:]:
        rec = json.loads(line)
        if rec.get("path") == target:
            [f] = [f for f in rec["functions"] if f["name"] == fn]
            return [(c["name"], c.get("path")) for c in f["calls"]]
    raise AssertionError(f"{target} not captured")


def test_typescript(tmp_path) -> None:
    assert _calls(tmp_path, {
        "src/a.ts": "export function load() {}\n",
        "src/b.ts": "export function load() {}\n",
        "src/main.ts": """import { load } from './a';
import * as b from './b';
export function run(items: string[]) { load(); b.load(); load(); items.push('x'); }
""",
    }, "src/main.ts", "run") == [("load", "src/a.ts"), ("load", "src/b.ts"), ("push", None)]


def test_typescript_unresolved_first_does_not_hide_the_resolved(tmp_path) -> None:
    assert _calls(tmp_path, {"src/v.ts": """export class V {
  check(warnings: string[]) { warnings.push('w'); this.push('e'); }
  private push(issue: string) {}
}
"""}, "src/v.ts", "check") == [("push", None), ("push", "src/v.ts")]


def test_python(tmp_path) -> None:
    assert _calls(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/a.py": "def save():\n    pass\n",
        "pkg/b.py": "def save():\n    pass\n",
        "pkg/main.py": "import pkg.a as a\nimport pkg.b as b\n\ndef run():\n    a.save()\n    b.save()\n",
    }, "pkg/main.py", "run") == [("save", "pkg/a.py"), ("save", "pkg/b.py")]
