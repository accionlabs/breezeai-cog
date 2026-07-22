"""ServiceHost parser (WCF ``.svc`` + ASMX ``.asmx``): directive parsing, route(rpc) emission,
CodeBehind IMPORTS resolution, capture-gating, honest-null cases, selection, schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.dotnet_servicehost.parser import ServiceHostParser
from breezeai_cog.schemas import FileRecord

# --- WCF .svc (ServiceHost / Service=) ---
SVC_FULL = (
    b'<%@ ServiceHost Language="C#" Service="Acme.Services.OrderService" '
    b'Factory="System.ServiceModel.Activation.ServiceHostFactory" '
    b'CodeBehind="OrderService.svc.cs" %>\n'
)
SVC_NO_CODEBEHIND = b'<%@ ServiceHost Service="A.B.OrderService" Factory="X.Y.F" %>\n'
SVC_MULTILINE = b'<%@ servicehost\n    service="A.B.MultiSvc"\n    codebehind="Multi.svc.cs" %>\n'
SVC_NO_CLASS = b'<%@ ServiceHost Language="C#" %>\n'

# --- ASMX .asmx (WebService / Class=) ---
ASMX_FULL = (
    b'<%@ WebService Language="C#" Class="Acme.Services.PricingService" '
    b'CodeBehind="PricingService.asmx.cs" %>\n'
)
ASMX_NO_CLASS = b'<%@ WebService Language="C#" %>\n'


def _parse(src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    ctx = ParseContext(
        path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture
    )
    return ServiceHostParser().parse_file(ctx)


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
    return ServiceHostParser().parse_file(ctx)


def _routes(rec: FileRecord):
    return [s for s in rec.statements if s.semanticType == "route"]


# --- .svc (WCF) ------------------------------------------------------------------


def test_svc_language_and_route() -> None:
    rec = _parse(SVC_FULL, "Services/OrderService.svc")
    assert rec.type == "code" and rec.language == "svc"  # own artifact, not csharp
    r = _routes(rec)[0]
    assert (r.framework, r.method, r.routeKind, r.nodeType) == ("wcf", "RPC", "rpc", "synthetic")
    assert r.handler == "Acme.Services.OrderService"  # concrete impl FQN (interface→impl)
    assert r.endpoint == "Services/OrderService.svc"
    assert r.parentId == rec.id
    assert rec.framework == "wcf"


def test_svc_attribute_order_and_no_codebehind() -> None:
    rec = _parse(SVC_NO_CODEBEHIND, "Services/Order.svc")
    assert _routes(rec)[0].handler == "A.B.OrderService"
    assert rec.importFiles == []  # no CodeBehind → no import edge


def test_svc_multiline_lowercase() -> None:
    rec = _parse(SVC_MULTILINE, "Services/Multi.svc")
    assert _routes(rec)[0].handler == "A.B.MultiSvc"


def test_svc_codebehind_resolves_to_import(tmp_path: Path) -> None:
    rec = _parse_repo(
        tmp_path,
        "Services/OrderService.svc",
        {
            "Services/OrderService.svc": SVC_FULL,
            "Services/OrderService.svc.cs": b"namespace Acme.Services { public class OrderService {} }",
        },
    )
    assert rec.importFiles == ["Services/OrderService.svc.cs"]


def test_svc_codebehind_missing_is_honest_null(tmp_path: Path) -> None:
    rec = _parse_repo(tmp_path, "Services/Multi.svc", {"Services/Multi.svc": SVC_MULTILINE})
    assert _routes(rec)[0].handler == "A.B.MultiSvc"  # route still emitted
    assert rec.importFiles == []  # unresolved CodeBehind → honest-null


# --- .asmx (ASMX) ----------------------------------------------------------------


def test_asmx_language_and_route() -> None:
    rec = _parse(ASMX_FULL, "Services/PricingService.asmx")
    assert rec.type == "code" and rec.language == "asmx"  # distinct from svc and csharp
    r = _routes(rec)[0]
    # ASMX shares the rpc shape but framework=asmx; handler = Class= (concrete impl → endpoint URL)
    assert (r.framework, r.method, r.routeKind, r.nodeType) == ("asmx", "RPC", "rpc", "synthetic")
    assert r.handler == "Acme.Services.PricingService"
    assert r.endpoint == "Services/PricingService.asmx"
    assert rec.framework == "asmx"


def test_asmx_codebehind_resolves_to_import(tmp_path: Path) -> None:
    rec = _parse_repo(
        tmp_path,
        "Services/PricingService.asmx",
        {
            "Services/PricingService.asmx": ASMX_FULL,
            "Services/PricingService.asmx.cs": b"namespace Acme.Services { public class PricingService {} }",
        },
    )
    assert rec.importFiles == ["Services/PricingService.asmx.cs"]  # Class join to [WebMethod] .cs


# --- honest-null / gating (both types) -------------------------------------------


def test_no_class_attribute_emits_nothing() -> None:
    assert _routes(_parse(SVC_NO_CLASS, "x/Empty.svc")) == []
    assert _routes(_parse(ASMX_NO_CLASS, "x/Empty.asmx")) == []


def test_no_directive_emits_nothing() -> None:
    assert _routes(_parse(b"not a host file\n", "x/Bogus.svc")) == []
    assert _routes(_parse(b"not a host file\n", "x/Bogus.asmx")) == []


def test_route_requires_capture_statements() -> None:
    for src, name in ((SVC_FULL, "A.svc"), (ASMX_FULL, "A.asmx")):
        rec = _parse(src, name, capture=False)
        assert _routes(rec) == [] and rec.framework is None  # gated
        assert rec.language in ("svc", "asmx")  # still captured for structure


def test_selection_claims_both_extensions() -> None:
    registry.clear()
    from breezeai_cog.parsers.csharp.parser import CSharpParser

    registry.register(CSharpParser())
    registry.register(ServiceHostParser())
    assert registry.select("A.svc", SVC_FULL).name == "dotnet-servicehost"
    assert registry.select("A.asmx", ASMX_FULL).name == "dotnet-servicehost"
    # code-behind stays C# (suffix is .cs, not .svc/.asmx)
    assert registry.select("A.svc.cs", b"class X {}").name == "csharp"
    assert registry.select("A.asmx.cs", b"class X {}").name == "csharp"
    registry.clear()


def test_output_validates() -> None:
    for src, name in ((SVC_FULL, "A.svc"), (ASMX_FULL, "A.asmx"), (SVC_NO_CLASS, "B.svc")):
        rec = _parse(src, name)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors
