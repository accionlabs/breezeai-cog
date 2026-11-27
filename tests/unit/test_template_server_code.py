"""The template splitter (``parsers/template_common/server_code.py``): a page's server code found
and split from its markup, for the JSP and ASP.NET dialects. JSP's own behaviour is covered by
``test_jsp_parser.py``; these tests cover the shared contract and the ASP.NET dialect."""

from __future__ import annotations

from breezeai_cog.parsers.template_common.server_code import (
    ASPNET,
    JSP,
    scan_islands,
    shadow_code,
    shadow_markup,
)
from breezeai_cog.parsers.treesitter import parse_source

PAGE = b"""<%@ Page Language="C#" CodeBehind="Orders.aspx.cs" %>
<%-- <% hidden(); %> --%>
<% if (User.IsAdmin) { %>
  <asp:Button ID="Save" OnClick="Save_Click" runat="server"/>
<% } %>
<p><%: Model.Name %> <%= Count %></p>
<asp:Label Text='<%# Eval("Qty") %>' runat="server"/>
<asp:Label Text='<%#: Item.Title %>' runat="server"/>
<asp:Literal Text="<%$ Resources:Title %>" runat="server"/>
<script>var id = '<%= Id %>';</script>
<script runat="server">
  protected void Save_Click(object s, EventArgs e) { Db.Save(); }
</script>
"""


def _kinds(src: bytes) -> list[tuple[str, int]]:
    lines = [0] + [i + 1 for i, b in enumerate(src) if b == 0x0A]
    return [(i.kind, sum(1 for s in lines if s <= i.start)) for i in scan_islands(src, ASPNET)]


def _lines(shadow: bytes) -> list[str]:
    return [line.strip() for line in shadow.decode().split("\n")]


def test_aspnet_islands_in_source_order() -> None:
    assert _kinds(PAGE) == [
        ("directive", 1), ("comment", 2), ("scriptlet", 3), ("scriptlet", 5),
        ("expression", 6), ("expression", 6), ("binding", 7), ("binding", 8),
        ("expression_builder", 9), ("expression", 10), ("declaration", 11),
    ]


def test_directive_name() -> None:
    assert scan_islands(PAGE, ASPNET)[0].name == "Page"


def test_shadows_keep_length_and_lines() -> None:
    islands = scan_islands(PAGE, ASPNET)
    for shadow in (shadow_markup(PAGE, islands), shadow_code(PAGE, islands, ASPNET)):
        assert len(shadow) == len(PAGE)
        assert shadow.count(b"\n") == PAGE.count(b"\n")


def test_markup_copy_keeps_the_markup_and_blanks_every_island() -> None:
    lines = _lines(shadow_markup(PAGE, scan_islands(PAGE, ASPNET)))
    assert lines[0] == lines[1] == lines[2] == ""
    assert lines[3] == '<asp:Button ID="Save" OnClick="Save_Click" runat="server"/>'
    assert lines[5].replace(" ", "") == "<p></p>"
    assert lines[8].replace(" ", "") == '<asp:LiteralText=""runat="server"/>'
    assert lines[9].replace(" ", "") == "<script>varid='';</script>"


def test_code_copy_holds_only_the_code() -> None:
    lines = _lines(shadow_code(PAGE, scan_islands(PAGE, ASPNET), ASPNET))
    assert lines[2] == "if (User.IsAdmin) {"
    assert lines[3] == ""  # markup
    assert lines[4] == "}"
    assert lines[5].split() == ["_=", "Model.Name", ";", "_=", "Count", ";"]
    assert lines[6] == '_= Eval("Qty") ;'
    assert lines[7] == "_= Item.Title ;"
    assert lines[8] == ""  # an expression builder is not code
    assert lines[1] == ""  # nor is a comment, even one holding <% %>
    assert lines[11] == "protected void Save_Click(object s, EventArgs e) { Db.Save(); }"


def test_code_copy_parses_as_csharp() -> None:
    code = shadow_code(PAGE, scan_islands(PAGE, ASPNET), ASPNET)
    root = parse_source("csharp", code, 0).root_node
    assert not root.has_error
    found = [(c.type, c.start_point[0] + 1) for s in root.named_children for c in s.named_children]
    assert ("if_statement", 3) in found
    assert ("expression_statement", 7) in found
    assert ("local_function_statement", 12) in found


def test_a_browser_script_is_not_server_code_but_its_islands_are() -> None:
    src = b"<script type='text/javascript'>var id = '<%= Id %>';</script>"
    assert [i.kind for i in scan_islands(src, ASPNET)] == ["expression"]


def test_an_unclosed_island_runs_to_the_end_of_the_page() -> None:
    (island,) = scan_islands(b"<p>x</p><% if (a) {", ASPNET)
    assert (island.kind, island.closed, island.body_end) == ("scriptlet", False, 19)


def test_jsp_has_no_server_scripts_or_aspnet_markers() -> None:
    src = b'<script runat="server">x();</script><%# a %><%: b %>'
    assert [i.kind for i in scan_islands(src, JSP)] == ["scriptlet", "scriptlet"]


def test_jsp_expression_wraps_as_java() -> None:
    src = b"<p><%= total(orders) %></p>"
    code = shadow_code(src, scan_islands(src, JSP), JSP)
    assert code.strip() == b"( total(orders) );"
