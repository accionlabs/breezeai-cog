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


def test_conventional_web_root_without_web_inf(analyze_files) -> None:
    # Servlet 3+ needs no web.xml, so these web roots have no WEB-INF folder.
    st = analyze_files({
        "nb/nbproject/project.xml": "<project/>",
        "nb/web/index.jsp": "<h1>NetBeans</h1>",
        "mvn/src/main/webapp/shop/list.jsp": "<h1>Maven</h1>",
        "ecl/WebContent/a.jsp": "<h1>Eclipse</h1>",
        "other/web/page.jsp": "<h1>a web/ folder of no project</h1>",
    }, templates=True)
    assert sorted((r.path, r.endpoint) for r in _eps(st)) == [
        ("ecl/WebContent/a.jsp", "/a.jsp"),
        ("mvn/src/main/webapp/shop/list.jsp", "/shop/list.jsp"),
        ("nb/web/index.jsp", "/index.jsp"),
    ]


def test_include_of_a_file_not_in_the_repo_is_an_external_import(analyze_files, tmp_path) -> None:
    files = {**WEB_XML, f"{WEB}/user/head.jspf": "x",
             f"{WEB}/user/list.jsp": ('<%@ include file="head.jspf" %>\n'
                                      '<%@ include file="nope.jspf" %>\n'
                                      '<jsp:include page="/common/missing.jsp"/>\n'
                                      '<jsp:include page="${dyn}"/>\n'
                                      '<%@ include file="../../../../../outside.jspf" %>\n')}
    analyze_files(files, templates=True)
    from breezeai_cog import iter_file_records

    rec = next(r for r in iter_file_records(tmp_path / "repo", capture_templates=True)
               if r.path.endswith("list.jsp"))
    assert rec.importFiles == [f"{WEB}/user/head.jspf"]
    # A dynamic path or one leaving the tree is neither a link nor an import.
    assert rec.externalImports == ["nope.jspf", "/common/missing.jsp"]


def test_dynamic_method_call_link_addresses_the_action(analyze_files) -> None:
    st = analyze_files({**WEB_XML, f"{WEB}/user/list.jsp": '<a href="login!save.action">s</a>\n'},
                       templates=True)
    got = [s.endpoint for s in st if s.semanticType == "route" and s.routeKind == "navigation"]
    assert got == ["/user/login.action"]
