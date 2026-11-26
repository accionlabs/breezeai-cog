"""C# ``Type.M()`` where the file names ``Type`` only as the call's receiver resolves to the file
declaring ``M`` (``parsers/csharp/static_calls.py``) — and only when nothing else can be meant."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

DOMAIN = {
    "Domain/ClauseRequest.cs": (
        "namespace Acme.Domain {\n  public class ClauseRequest : RequestBase {\n"
        "    public static ClauseRequest Create(string id) { return null; }\n  }\n}\n"),
    "Domain/RequestBase.cs": (
        "namespace Acme.Domain {\n  public class RequestBase {\n"
        "    public static void Audit() { }\n  }\n}\n"),
    "Domain/Repository.cs": (
        "namespace Acme.Domain {\n  public class Repository {\n"
        "    public static int Get() { return 1; }\n  }\n}\n"),
    "Other/ClauseRequest.cs": (
        "namespace Acme.Other {\n  public class Clash {\n    public static int Create() { return 1; }\n  }\n}\n"),
    "Twice/A.cs": "namespace Acme.A {\n  public class Dup {\n    public static int Run() { return 1; }\n  }\n}\n",
    "Twice/B.cs": "namespace Acme.B {\n  public class Dup {\n    public static int Run() { return 2; }\n  }\n}\n",
}


@pytest.fixture
def calls_of(tmp_path: Path):
    def run(src: str) -> list[tuple[str, str | None]]:
        repo = tmp_path / "repo"
        for rel, text in {**DOMAIN, "App/Svc.cs": src}.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        (rec,) = [r for r in iter_file_records(repo) if r.path == "App/Svc.cs"]
        return [(c.name, c.path) for f in rec.functions for c in f.calls]

    return run


def test_static_call_on_a_type_of_another_file(calls_of) -> None:
    got = calls_of("using Acme.Domain;\nnamespace Acme.App {\n  class Svc {\n"
                   "    void Go() { ClauseRequest.Create(\"x\"); ClauseRequest.Audit(); }\n  }\n}\n")
    assert ("Create", "Domain/ClauseRequest.cs") in got
    assert ("Audit", "Domain/RequestBase.cs") in got      # inherited: the declaring file


def test_same_file_type(calls_of) -> None:
    got = calls_of("namespace Acme.App {\n  static class Helper { public static void Init() { } }\n"
                   "  class Svc { static Svc() { Helper.Init(); } void Go() { Helper.Missing(); } }\n}\n")
    assert ("Init", "App/Svc.cs") in got
    assert ("Missing", None) in got


def test_a_value_named_like_the_type_is_no_type(calls_of) -> None:
    got = calls_of("using Acme.Domain;\nnamespace Acme.App {\n  class Svc {\n"
                   "    public IRepo Repository { get; set; }\n"
                   "    void Go(object ClauseRequest) { Repository.Get(); ClauseRequest.Create(\"x\"); }\n  }\n}\n")
    assert ("Get", None) in got and ("Create", None) in got


def test_type_not_in_scope_ambiguous_or_method_not_declared(calls_of) -> None:
    got = calls_of("using Acme.A;\nusing Acme.B;\nnamespace Acme.App {\n  class Svc {\n"
                   "    void Go() { Dup.Run(); }\n"
                   "    void Out() { Repository.Get(); }\n  }\n}\n")
    assert ("Run", None) in got                   # Dup: two classes in scope
    assert ("Get", None) in got                   # Acme.Domain not imported
    got = calls_of("using Acme.Domain;\nnamespace Acme.App {\n  class Svc {\n"
                   "    void Go() { ClauseRequest.ToString(); }\n  }\n}\n")
    assert ("ToString", None) in got              # not declared in the repository


def test_method_of_a_same_named_class_in_another_namespace(tmp_path: Path) -> None:
    # two classes `Util` (one base): the index merges them; `Only` is declared by the other one
    repo = tmp_path / "r"
    files = {
        "A/Util.cs": "namespace Ns.A {\n  public class Util {\n    public static int Mine() { return 1; }\n  }\n}\n",
        "B/Util.cs": "namespace Ns.B {\n  public class Util {\n    public static int Only() { return 2; }\n  }\n}\n",
        "App/Svc.cs": ("using Ns.A;\nnamespace Ns.App {\n  class Svc {\n"
                       "    void Go() { Util.Mine(); Util.Only(); }\n  }\n}\n"),
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (rec,) = [r for r in iter_file_records(repo) if r.path == "App/Svc.cs"]
    got = [(c.name, c.path) for f in rec.functions for c in f.calls]
    assert ("Mine", "A/Util.cs") in got
    assert ("Only", None) in got
