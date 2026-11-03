"""JSP role table: docs/entry-point-roles/jsp.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

WEB = "src/main/webapp"
WEB_XML = {f"{WEB}/WEB-INF/web.xml": "<web-app/>"}


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def test_served_page_is_binding(analyze_files) -> None:
    st = analyze_files({**WEB_XML, f"{WEB}/shop/index.jsp": "<%@ page %>\n<h1>Shop</h1>\n"},
                       templates=True)
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.framework, r.endpoint) == (
        "route", "GET", "page", "jsp", "/shop/index.jsp")
    assert (r.handler, r.handlerLine, r.handlerPath, r.startLine, r.nodeType) == (
        "index.jsp", 1, None, 1, "document")


def test_jspx_page_is_binding(analyze_files) -> None:
    st = analyze_files({**WEB_XML, f"{WEB}/doc.jspx": '<jsp:root version="2.0"/>'},
                       templates=True)
    assert [r.endpoint for r in _eps(st)] == ["/doc.jspx"]


def test_web_inf_page_is_none(analyze_files) -> None:
    st = analyze_files({**WEB_XML, f"{WEB}/WEB-INF/jsp/login.jsp": "<h1>Login</h1>"},
                       templates=True)
    assert _eps(st) == []


def test_page_outside_a_web_application_is_none(analyze_files) -> None:
    assert _eps(analyze_files({"pages/index.jsp": "<h1>x</h1>"}, templates=True)) == []


def test_fragments_and_tag_files_are_none(analyze_files) -> None:
    st = analyze_files({
        **WEB_XML,
        f"{WEB}/parts/header.jspf": "<header/>",
        f"{WEB}/tags/box.tag": '<%@ tag %><div class="box"/>',
        f"{WEB}/tags/row.tagx": '<jsp:root version="2.0"><jsp:directive.tag/></jsp:root>',
    }, templates=True)
    assert _eps(st) == []


def test_includes_are_none(analyze_files) -> None:
    st = analyze_files({
        **WEB_XML,
        f"{WEB}/a.jsp": '<%@ include file="b.jspf" %><jsp:include page="c.jsp"/>',
        f"{WEB}/b.jspf": "b",
        f"{WEB}/c.jsp": "c",
    }, templates=True)
    assert sorted(r.endpoint for r in _eps(st)) == ["/a.jsp", "/c.jsp"]  # the pages only
    assert not any(s.routeKind == "navigation" for s in st)


def test_links_are_outbound_navigation(analyze_files) -> None:
    st = analyze_files({
        **WEB_XML,
        f"{WEB}/shop/index.jsp": ('<a href="list.jsp">l</a><form action="/login.do"></form>'
                                  '<a href="${next}.jsp">dyn</a><a href="https://x.org/a.jsp">x</a>'
                                  '<a href="logo.png">img</a>'),
        f"{WEB}/shop/list.jsp": "list",
    }, templates=True)
    navs = [(s.name, s.endpoint, s.framework) for s in st if s.routeKind == "navigation"]
    assert navs == [("a", "/shop/list.jsp", "jsp"), ("form", "/login.do", "jsp")]
    # the link and the page it reaches carry the same address
    assert "/shop/list.jsp" in {r.endpoint for r in _eps(st)}


def test_fixture_page_is_none(analyze_files) -> None:
    st = analyze_files({**WEB_XML, f"{WEB}/fixtures/demo.jsp": '<a href="x.jsp">x</a>'},
                       templates=True)
    assert _eps(st) == [] and not any(s.routeKind == "navigation" for s in st)
