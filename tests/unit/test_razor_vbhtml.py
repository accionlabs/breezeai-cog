"""VB Razor views (``.vbhtml``): a File with the ``.cshtml`` template statements (from a text scan,
``synthetic``), and inline jQuery calls with the layout-chain evidence."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.razor.vbhtml import collect_vbhtml_statements

VIEW = b"""@ModelType IEnumerable(Of Shop.Order)
@Code
    ViewData("Title") = "Orders"
    Dim n = @Model.Count()
End Code
<p>@Html.ActionLink("Create New", "Create") or mail sales@example.com, @@twitter</p>
@For Each item In Model
    @<tr><td>@item.Total</td><td>@Html.DisplayFor(Function(m) item.Name)</td></tr>
Next
@If Model.Any() Then
    @<p>@Model.Count() orders</p>
End If
@* @Hidden.Comment *@
<style>@media print { }</style>
@Html.Unclosed(
"""


def test_a_vb_view_gets_the_template_statements() -> None:
    got = [(s.startLine, s.nodeType, s.name, s.text, s.semanticType)
           for s in collect_vbhtml_statements(VIEW, "Index.vbhtml", "f", set(), 8000)]
    assert got == [
        (1, "synthetic", "IEnumerable(Of Shop.Order)", "@ModelType IEnumerable(Of Shop.Order)", None),
        (6, "synthetic", 'Html.ActionLink("Create New", "Create")',
         '@Html.ActionLink("Create New", "Create")', None),
        (7, "synthetic", None, "@For Each item In Model", None),
        (8, "synthetic", "item.Total", "@item.Total", None),
        (8, "synthetic", "Html.DisplayFor(Function(m) item.Name)",
         "@Html.DisplayFor(Function(m) item.Name)", None),
        (10, "synthetic", None, "@If Model.Any() Then", None),
        (11, "synthetic", "Model.Count()", "@Model.Count()", None),
    ]
    # Not code: the @Code body, an e-mail address, @@, a comment, a style body, `@<tr>` markup,
    # and an expression whose parenthesis does not close on its line.


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return tmp_path


def test_a_vbhtml_file_is_a_razor_template_only_with_template_capture(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"Views/Home/Index.vbhtml": VIEW.decode()})
    assert not list(iter_file_records(root))
    (rec,) = iter_file_records(root, capture_templates=True)
    assert (rec.path, rec.language, rec.framework, rec.uiRole) == (
        "Views/Home/Index.vbhtml", "html", "razor", "template")
    assert rec.statements and all(s.nodeType == "synthetic" for s in rec.statements)


_LAYOUT = (
    '<body>@RenderBody()\n<script src="~/Scripts/jquery-3.4.1.min.js"></script>\n'
    '@RenderSection("scripts", required:=False)</body>\n'
)


def test_inline_jquery_in_a_vb_view_uses_its_layout_chain(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "Views/Shared/_Layout.vbhtml": _LAYOUT,
        "Views/Shared/_Plain.vbhtml": "<body>@RenderBody()</body>\n",
        "Views/_ViewStart.vbhtml": '@Code\n    Layout = "~/Views/Shared/_Layout.vbhtml"\nEnd Code\n',
        # through _ViewStart → _Layout, which loads jQuery
        "Views/Orders/Index.vbhtml": '<script>\n$.get("/api/orders");\n</script>\n',
        # its own layout, without jQuery: no evidence
        "Views/Orders/Plain.vbhtml": '@Code\n    Layout = "_Plain"\nEnd Code\n<script>\n$.get("/api/plain");\n</script>\n',
        # Layout = Nothing opts out
        "Views/Orders/Bare.vbhtml": '@Code\n    Layout = Nothing\nEnd Code\n<script>\n$.get("/api/bare");\n</script>\n',
    })
    calls = {rec.path: [(s.startLine, s.method, s.endpoint) for s in rec.statements
                        if s.semanticType == "api_call"]
             for rec in iter_file_records(root, capture_templates=True)}
    assert {p: c for p, c in calls.items() if c} == {"Views/Orders/Index.vbhtml": [(2, "GET", "/api/orders")]}
