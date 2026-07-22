"""WCF ``.svc`` ServiceHost parser: directive parsing, route(rpc) emission, CodeBehind
IMPORTS resolution, capture-gating, honest-null cases, selection, and schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.svc.parser import SvcHostParser
from breezeai_cog.schemas import FileRecord

# Typical: all three attributes, CodeBehind that will resolve on disk.
SVC_FULL = (
    b'<%@ ServiceHost Language="C#" Service="KUCare.Services.AttendanceService" '
    b'Factory="System.ServiceModel.Activation.ServiceHostFactory" '
    b'CodeBehind="AttendanceService.svc.cs" %>\n'
)
# Attribute order swapped, no CodeBehind.
SVC_NO_CODEBEHIND = b'<%@ ServiceHost Service="A.B.OrderService" Factory="X.Y.F" %>\n'
# Multi-line + lowercase directive/attrs.
SVC_MULTILINE = b'<%@ servicehost\n    service="A.B.MultiSvc"\n    codebehind="Multi.svc.cs" %>\n'
# ServiceHost present but no Service attribute → nothing to bind.
SVC_NO_SERVICE = b'<%@ ServiceHost Language="C#" %>\n'


def _parse(src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(
        path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture
    )
    return SvcHostParser().parse_file(ctx)


def _parse_repo(
    root: Path, target: str, files: dict[str, bytes], *, capture: bool = True
) -> FileRecord:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    ctx = ParseContext(
        path=target,
        abs_path=root / target,
        source=(root / target).read_bytes(),
        repo_root=root,
        capture_statements=capture,
    )
    return SvcHostParser().parse_file(ctx)


def _routes(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "route"]


def test_file_captured_with_svc_language() -> None:
    rec = _parse(SVC_FULL, "Services/AttendanceService.svc")
    assert rec.type == "code"
    assert rec.language == "svc"  # own artifact — not folded into csharp rollups


def test_servicehost_emits_rpc_route() -> None:
    rec = _parse(SVC_FULL, "Services/AttendanceService.svc")
    routes = _routes(rec)
    assert len(routes) == 1
    r = routes[0]
    assert (r.framework, r.method, r.routeKind, r.nodeType) == ("wcf", "RPC", "rpc", "synthetic")
    assert r.handler == "KUCare.Services.AttendanceService"  # concrete impl FQN (interface→impl)
    assert r.endpoint == "Services/AttendanceService.svc"
    assert r.parentId == rec.id  # the .svc file owns the endpoint
    assert rec.framework == "wcf"


def test_attribute_order_and_no_codebehind() -> None:
    rec = _parse(SVC_NO_CODEBEHIND, "Services/Order.svc")
    routes = _routes(rec)
    assert len(routes) == 1
    assert routes[0].handler == "A.B.OrderService"
    assert rec.importFiles == []  # no CodeBehind → no import edge


def test_multiline_lowercase_directive() -> None:
    rec = _parse(SVC_MULTILINE, "Services/Multi.svc")
    routes = _routes(rec)
    assert len(routes) == 1
    assert routes[0].handler == "A.B.MultiSvc"


def test_codebehind_resolves_to_import(tmp_path: Path) -> None:
    rec = _parse_repo(
        tmp_path,
        "Services/AttendanceService.svc",
        {
            "Services/AttendanceService.svc": SVC_FULL,
            "Services/AttendanceService.svc.cs": b"namespace KUCare.Services { public class AttendanceService {} }",
        },
    )
    assert rec.importFiles == ["Services/AttendanceService.svc.cs"]  # CodeBehind → IMPORTS edge


def test_codebehind_missing_file_is_honest_null(tmp_path: Path) -> None:
    # CodeBehind names a file that does not exist on disk → no dangling import edge.
    rec = _parse_repo(tmp_path, "Services/Multi.svc", {"Services/Multi.svc": SVC_MULTILINE})
    assert _routes(rec)[0].handler == "A.B.MultiSvc"  # route still emitted
    assert rec.importFiles == []  # unresolved CodeBehind → honest-null


def test_no_service_attribute_emits_nothing() -> None:
    rec = _parse(SVC_NO_SERVICE, "Services/Empty.svc")
    assert _routes(rec) == []
    assert rec.framework is None


def test_no_directive_emits_nothing() -> None:
    rec = _parse(b"just some text, not a servicehost file\n", "Services/Bogus.svc")
    assert _routes(rec) == []


def test_route_requires_capture_statements() -> None:
    rec = _parse(SVC_FULL, "Services/AttendanceService.svc", capture=False)
    assert _routes(rec) == []
    assert rec.framework is None  # gated — no route, no framework label
    assert rec.language == "svc"  # file still captured for structure


def test_selection_claims_svc() -> None:
    registry.clear()
    from breezeai_cog.parsers.csharp.parser import CSharpParser

    registry.register(CSharpParser())
    registry.register(SvcHostParser())
    assert registry.select("A.svc", SVC_FULL).name == "svc"
    assert registry.select("A.svc.cs", b"class X {}").name == "csharp"  # code-behind stays C#
    registry.clear()


def test_output_validates() -> None:
    for src, name in ((SVC_FULL, "A.svc"), (SVC_NO_SERVICE, "B.svc")):
        rec = _parse(src, name)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors
