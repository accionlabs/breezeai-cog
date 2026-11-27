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


def test_page_template_forms_give_the_served_address(tmp_path) -> None:
    # "/x" and "~/x" both replace the page's folder address (ASP.NET serves "~/x" at "/x"); a
    # relative template joins it; a bare "~" or "~x" is no valid route and stays empty.
    cases = {
        '"/orders/{id}"': "/orders/{id}",
        '"~/orders/{id}"': "/orders/{id}",
        '"~/"': "/",
        '"~"': None,
        '"~orders"': None,
        '"{id:int}"': "/Orders/Edit/{id:int}",
        "": "/Orders/Edit",
    }
    (tmp_path / "Pages" / "Orders").mkdir(parents=True)
    for template, endpoint in cases.items():
        source = f"@page {template}\n@model EditModel\n<h1>Edit</h1>\n".encode()
        rec = _parse(tmp_path, "Pages/Orders/Edit.cshtml", source)
        routes = [s for s in rec.statements if s.semanticType == "route"]
        assert [r.endpoint for r in routes] == [endpoint], template


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


# ── Step 2: @code { } methods → Functions ─────────────────────────────────────

_BLAZOR = b"""@page "/counter"
<button @onclick="Remove">X</button>
@code {
  private int count = 0;
  void Remove(int id) { count--; }
  public async Task Load() { await Fetch(); }
}"""


def test_code_methods_are_functions(tmp_path) -> None:
    rec = _parse(tmp_path, "Counter.razor", _BLAZOR)
    fns = {f.name: f for f in rec.functions}
    assert {"Remove", "Load"} <= set(fns)
    assert fns["Remove"].type == "method"
    assert [(p.name, p.type) for p in fns["Remove"].params] == [("id", "int")]
    assert fns["Load"].returnType == "Task"


def test_handler_resolves_to_code_method(tmp_path) -> None:
    # @onclick="Remove" (a statement handler) matches a Function declared in @code — the join.
    rec = _parse(tmp_path, "Counter.razor", _BLAZOR)
    handlers = {s.handler for s in rec.statements if s.nodeType == "razor_html_attribute"}
    assert "Remove" in handlers
    assert "Remove" in {f.name for f in rec.functions}


def test_code_functions_require_capture(tmp_path) -> None:
    rec = _parse(tmp_path, "Counter.razor", _BLAZOR, capture=False)
    assert rec.functions == []


def test_blazor_page_handler_is_its_own_file(tmp_path) -> None:
    # A Blazor component is its whole file: the page's handler starts at line 1, in this file.
    rec = _parse(tmp_path, "Counter.razor", _BLAZOR)
    [r] = [s for s in rec.statements if s.routeKind == "page"]
    assert (r.endpoint, r.handler, r.handlerLine, r.handlerPath) == ("/counter", "Counter", 1, None)
    # A Razor Page's @model class is in another file, not located yet: no line.
    rec = _parse(tmp_path, "Orders.cshtml", _RAZOR)
    [r] = [s for s in rec.statements if s.routeKind == "page"]
    assert r.handlerLine is None


# ── links, form posts and page scripts (shared readers) ──────────────────────────────────────


def _markup(rec) -> list[tuple[int, str | None, str | None, str | None]]:
    return [(s.startLine, s.semanticType, s.method, s.endpoint)
            for s in rec.statements if s.nodeType in ("start_tag", "self_closing_tag")]


def _calls(rec) -> list[tuple[int, str | None, str | None]]:
    return [(s.startLine, s.method, s.endpoint) for s in rec.statements
            if s.semanticType == "api_call" and s.nodeType not in ("start_tag", "self_closing_tag")]


def test_tag_helper_forms_and_links_are_records(tmp_path) -> None:
    page = (b'@page "/orders"\n'
            b'<form method="post" asp-page-handler="Save"><input asp-for="Email" /></form>\n'
            b'@if (Model.IsAdmin)\n{\n'
            b'    <a asp-controller="Home" asp-action="Index">Home</a>\n'
            b'}\n'
            b'<form asp-action="Find" asp-route-id="@Model.Id">f</form>\n'
            b'<a asp-page="/Orders/Edit" asp-route-id="3">Edit</a>\n')
    rec = _parse(tmp_path, "Orders.cshtml", page)
    assert _markup(rec) == [
        (2, "api_call", "POST", None),
        (5, "route", None, None),
        (7, "api_call", None, None),
        (8, "route", None, None),
    ]
    assert all(s.routeKind == "navigation" for s in rec.statements if s.nodeType == "start_tag"
               and s.semanticType == "route")


def test_plain_links_and_forms_in_a_razor_page(tmp_path) -> None:
    page = (b'<a href="/help.html">Help</a>\n'
            b'<a href="@Url.Action("Index")">Home</a>\n'
            b'<a href="~/about">About</a>\n'
            b'<form action="/contact/send" method="post"></form>\n'
            b'<a asp-for="x">not a link target</a>\n')
    assert _markup(_parse(tmp_path, "View.cshtml", page)) == [
        (1, "route", None, "/help.html"),
        (2, "route", None, None),
        (3, "route", None, None),
        (4, "api_call", "POST", "/contact/send"),
    ]


def test_markup_inside_csharp_is_not_read(tmp_path) -> None:
    page = (b'@{ var link = "<a href=\'/fake\'>x</a>"; }\n'
            b'@code { string f = "<form action=\'/fake\'></form>"; }\n'
            b'@* <a href="/commented">c</a> *@\n'
            b'@foreach (var o in Model.Orders) { <a href="/orders">@o.Name</a> }\n')
    assert _markup(_parse(tmp_path, "View.cshtml", page)) == [(4, "route", None, "/orders")]


def test_blazor_navlink_is_navigation(tmp_path) -> None:
    page = (b'@page "/nav"\n<NavLink href="/counter" Match="NavLinkMatch.All">Counter</NavLink>\n'
            b'<NavLink href="fetchdata">Fetch</NavLink>\n<a href="/help">Help</a>\n')
    assert _markup(_parse(tmp_path, "Nav.razor", page)) == [
        (2, "route", None, "/counter"),
        (3, "route", None, None),
        (4, "route", None, "/help"),
    ]


def test_page_scripts_are_read_by_every_detector(tmp_path) -> None:
    page = (b'@model OrderModel\n<script>\n'
            b'  var id = @Model.Id;\n'
            b"  var API = '/api/orders';\n"
            b"  fetch('/api/orders/' + id);\n"
            b"  axios.post(API, {});\n"
            b"  fetch('/api/user/@Model.UserId');\n"
            b"  fetch('@Url.Action(\"Save\")');\n"
            b'</script>\n')
    rec = _parse(tmp_path, "View.cshtml", page)
    assert _calls(rec) == [
        (5, "GET", "/api/orders/{id}"),
        (6, "POST", "/api/orders"),
        (7, "GET", None),
        (8, "GET", None),
    ]
    assert [s.text for s in rec.statements if s.startLine == 7 and s.semanticType == "api_call"] == [
        "fetch('/api/user/@Model.UserId');"
    ]


def test_page_scripts_and_markup_need_statement_capture(tmp_path) -> None:
    page = b'<a href="/x">x</a>\n<script>fetch("/api/x");</script>\n'
    assert _parse(tmp_path, "View.cshtml", page, capture=False).statements == []


def test_a_razor_page_links_its_script_files(tmp_path) -> None:
    (tmp_path / "wwwroot" / "js").mkdir(parents=True)
    (tmp_path / "wwwroot" / "js" / "site.js").write_text("x();")
    (tmp_path / "Pages").mkdir()
    page = b'<script src="~/js/site.js"></script>\n<script src="/js/site.js"></script>\n'
    rec = _parse(tmp_path, "Pages/Index.cshtml", page)
    assert rec.importFiles == []  # ~/ and a rooted path from Pages/ match no file there
    rec = _parse(tmp_path, "wwwroot/index.cshtml", page)
    assert rec.importFiles == ["wwwroot/js/site.js"]


# ── layout and partial views as file links; a line-final @expr keeps to its own line ──────


def _views(tmp_path, files: dict[str, bytes], target: str) -> FileRecord:
    for name, src in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(src)
    return _parse(tmp_path, target, files[target])


def test_layout_and_partials_link_to_the_shared_views(tmp_path) -> None:
    page = (b'@{ Layout = "_Layout"; }\n<partial name="_Menu" />\n'
            b'@await Html.PartialAsync("_Footer")\n<h1>Home</h1>\n')
    rec = _views(tmp_path, {
        "Views/Home/Index.cshtml": page,
        "Views/Shared/_Layout.cshtml": b"@RenderBody()\n",
        "Views/Shared/_Menu.cshtml": b"<nav></nav>\n",
        "Views/Shared/_Footer.cshtml": b"<footer></footer>\n",
    }, "Views/Home/Index.cshtml")
    assert rec.importFiles == ["Views/Shared/_Layout.cshtml", "Views/Shared/_Menu.cshtml",
                               "Views/Shared/_Footer.cshtml"]


def test_view_in_its_own_folder_wins_over_shared(tmp_path) -> None:
    rec = _views(tmp_path, {
        "Views/Home/Index.cshtml": b'@Html.Partial("_Row")\n',
        "Views/Home/_Row.cshtml": b"<tr></tr>\n",
        "Views/Shared/_Row.cshtml": b"<tr></tr>\n",
    }, "Views/Home/Index.cshtml")
    assert rec.importFiles == ["Views/Home/_Row.cshtml"]


def test_razor_pages_layout_path_from_the_app_root(tmp_path) -> None:
    rec = _views(tmp_path, {
        "App/Pages/Admin/Index.cshtml": b'@{ Layout = "~/Pages/Shared/_Admin.cshtml"; }\n',
        "App/Pages/Shared/_Admin.cshtml": b"@RenderBody()\n",
    }, "App/Pages/Admin/Index.cshtml")
    assert rec.importFiles == ["App/Pages/Shared/_Admin.cshtml"]


def test_layout_from_a_variable_or_a_missing_view_is_not_linked(tmp_path) -> None:
    rec = _views(tmp_path, {
        "Views/Home/Index.cshtml": b'@{ Layout = name; }\n<partial name="_Missing" />\n',
    }, "Views/Home/Index.cshtml")
    assert rec.importFiles == []


def test_line_final_expression_does_not_take_the_next_line(tmp_path) -> None:
    rec = _parse(tmp_path, "Index.cshtml",
                 b'@Html.Partial("_Footer")\n<h1>Home</h1>\n<p>Hi @user.Name.</p>\n')
    exprs = [(s.name, s.startLine, s.endLine, s.text) for s in rec.statements
             if s.nodeType == "razor_implicit_expression"]
    assert exprs == [('Html.Partial("_Footer")', 1, 1, '@Html.Partial("_Footer")'),
                     ("user.Name", 3, 3, "@user.Name")]


def test_razor_page_and_area_fall_back_to_views_shared(tmp_path) -> None:
    files = {
        "Web/Pages/_ViewStart.cshtml": b'@{ Layout = "_Layout"; }\n',
        "Web/Areas/Identity/Pages/Account/Login.cshtml": b'<partial name="_Scripts" />\n',
        "Web/Areas/Identity/Pages/_ViewStart.cshtml":
            b'@{ Layout = "/Views/Shared/_Layout.cshtml"; }\n',
        "Web/Views/Shared/_Layout.cshtml": b"@RenderBody()\n",
        "Web/Views/Shared/_Scripts.cshtml": b"<script></script>\n",
    }
    assert _views(tmp_path, files, "Web/Pages/_ViewStart.cshtml").importFiles == [
        "Web/Views/Shared/_Layout.cshtml"]
    assert _views(tmp_path, files, "Web/Areas/Identity/Pages/Account/Login.cshtml").importFiles \
        == ["Web/Views/Shared/_Scripts.cshtml"]
    assert _views(tmp_path, files, "Web/Areas/Identity/Pages/_ViewStart.cshtml").importFiles == [
        "Web/Views/Shared/_Layout.cshtml"]
