"""Python code outside functions makes calls (anonymous functions phase 2, Rule 3): a module's
own code gives ``File.calls``, a class body's own code ``Class.calls``; each def's body, and its
decorators and parameter defaults, stay its own."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records


def _records(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return {r.path: r for r in iter_file_records(repo)}


TASKS = "def extract(**kw):\n    return 1\n\ndef load(**kw):\n    return 2\n\ndef retry(n):\n    return lambda f: f\n"


def test_module_code_gives_file_calls(tmp_path: Path) -> None:
    dag = _records(tmp_path, {"tasks.py": TASKS, "dag.py": (
        "from airflow import DAG\nfrom tasks import extract, load, retry\n\n"
        "with DAG('etl') as dag:\n    a = extract(task_id='a')\n    b = load(task_id='b')\n    a >> b\n\n"
        "handler = lambda e: load(e=e)\n\n"
        "@retry(3)\ndef run(cb=extract()):\n    return load()\n\n"
        "if __name__ == '__main__':\n    run()\n")})["dag.py"]
    calls = [(c.name, c.path) for c in dag.calls]
    assert ("DAG", None) in calls
    assert ("extract", "tasks.py") in calls and ("load", "tasks.py") in calls
    assert ("run", "dag.py") in calls                 # `if __name__ == "__main__":`
    assert ("retry", "tasks.py") not in calls         # a decorator is the def's, not a call here
    (run,) = [f for f in dag.functions if f.name == "run"]
    assert ("load", "tasks.py") in [(c.name, c.path) for c in run.calls]


def test_class_body_gives_class_calls(tmp_path: Path) -> None:
    rec = _records(tmp_path, {"tasks.py": TASKS, "m.py": (
        "from tasks import extract, load\n\n"
        "class Job:\n    first = extract()\n    on_done = staticmethod(lambda r: load(r=r))\n"
        "    def run(self):\n        return load()\n"
        "    class Inner:\n        x = load()\n")})["m.py"]
    by = {c.name: [(x.name, x.path) for x in (c.calls or [])] for c in rec.classes}
    assert ("extract", "tasks.py") in by["Job"] and ("load", "tasks.py") in by["Job"]
    assert by["Inner"] == [("load", "tasks.py")]
    assert rec.calls is None                           # nothing runs at module level here


def test_module_without_own_calls_has_none(tmp_path: Path) -> None:
    rec = _records(tmp_path, {"tasks.py": TASKS})["tasks.py"]
    assert rec.calls is None
