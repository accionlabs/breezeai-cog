"""Razor (.cshtml / .razor) parser: @page routes, @on* handler wiring, @Model interpolations,
@foreach/@if control flow, @model, selection, and schema conformance."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.razor.parser import RazorParser
from breezeai_cog.schemas import FileRecord

_RAZOR = b"""@page "/orders/{id}"
@model OrderModel
<h1>@Model.Title</h1>
@foreach (var o in Model.Orders) { <div @onclick="Load">@o.Name</div> }
<button @onclick="Remove">X</button>
@code { void Load() {} void Remove() {} }"""


def _parse(tmp_path, name: str, source: bytes, *, capture=True) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(source)
    ctx = ParseContext(
        path=name, abs_path=p, source=source, repo_root=tmp_path, capture_statements=capture
    )
    return RazorParser().parse_file(ctx)


def test_file_role_and_framework(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    assert rec.language == "html" and rec.framework == "razor"
    assert rec.uiRole == "component"  # .razor is a Blazor component
    assert _parse(tmp_path, "View.cshtml", _RAZOR).uiRole == "template"  # .cshtml is a view


def test_page_directive_is_route(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    r = routes[0]
    assert r.nodeType == "razor_page_directive" and r.routeKind == "page"
    assert r.endpoint == "/orders/{id}" and r.framework == "razor"


def test_event_handler_wiring(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    events = {s.handler for s in rec.statements if s.nodeType == "razor_html_attribute"}
    assert events == {"Load", "Remove"}
    assert all(s.name == "onclick" for s in rec.statements if s.nodeType == "razor_html_attribute")


def test_interpolation_and_controlflow(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    interps = {s.name for s in rec.statements if s.nodeType == "razor_implicit_expression"}
    assert {"Model.Title", "o.Name"} <= interps
    assert any(s.nodeType == "razor_foreach" for s in rec.statements)
    # template constructs carry a real nodeType and no semanticType (never synthetic)
    non_route = [s for s in rec.statements if s.semanticType is None]
    assert non_route and all(s.nodeType != "synthetic" for s in non_route)


def test_model_directive(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    assert any(
        s.nodeType == "razor_model_directive" and s.name == "OrderModel" for s in rec.statements
    )


def test_route_requires_capture(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR, capture=False)
    assert rec.framework == "razor"  # File-level identity is not gated
    assert rec.statements == []


def test_selection_and_capabilities() -> None:
    registry.clear()
    registry.register(RazorParser())
    assert registry.select("Orders.razor", _RAZOR).name == "razor"
    assert registry.select("View.cshtml", _RAZOR).name == "razor"
    caps = registry.capabilities()
    assert {".cshtml", ".razor"} <= set(caps["extensions"]) and "razor" in caps["frameworks"]
    registry.clear()


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, "Orders.razor", _RAZOR)
    errors = list(
        Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
            json.loads(to_line(rec))
        )
    )
    assert not errors, errors
