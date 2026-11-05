"""JSP parser: every JSP version from 1.0 to Jakarta Pages 4.0, standard and XML syntax, tag
files, EL rules, legacy encodings, edges, the template gate and schema conformance.

Fixtures are written for these tests; each version section uses the constructs that version
introduced, and every page must parse without error whatever its version."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog import iter_file_records
from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.jsp.parser import JspParser
from breezeai_cog.parsers.jsp.scanner import decode_source, scan_islands
from breezeai_cog.schemas import FileRecord


def _repo(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(src if isinstance(src, bytes) else src.encode())
    return tmp_path


def _parse(repo: Path, rel: str, *, capture: bool = True, index: bool = True) -> FileRecord:
    parser = JspParser()
    ctx = ParseContext(
        path=rel, abs_path=repo / rel, source=(repo / rel).read_bytes(), repo_root=repo,
        capture_statements=capture,
        resolution_index=parser.build_index(repo, []) if index else None,
    )
    return parser.parse_file(ctx)


def _types(rec: FileRecord, node_type: str) -> list:
    return [s for s in rec.statements if s.nodeType == node_type]


def _names(rec: FileRecord, node_type: str) -> list:
    return [s.name for s in _types(rec, node_type)]


def _jsp_kind(s) -> str | None:
    """What a scanner-found (``synthetic``) record is, told apart by its syntax."""
    if s.nodeType != "synthetic":
        return None
    if s.semanticType == "comment":
        return "comment"
    text = s.text.lstrip()
    if text.startswith(("<%@", "<jsp:directive")):
        return "directive"
    if text.startswith(("${", "#{")):
        return "el"
    return None


def _jsp(rec: FileRecord, kind: str) -> list:
    return [s for s in rec.statements if _jsp_kind(s) == kind]


def _jsp_names(rec: FileRecord, kind: str) -> list:
    return [s.name for s in _jsp(rec, kind)]


def _valid(rec: FileRecord) -> None:
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
        json.loads(to_line(rec))))
    assert not errors, errors


# ── file identity ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,role", [
    ("a.jsp", "template"), ("a.jspx", "template"), ("a.jspf", "template"),
    ("a.tag", "component"), ("a.tagx", "component"),
])
def test_file_identity(tmp_path, name, role) -> None:
    rec = _parse(_repo(tmp_path, {name: "<%@ page %>${x}"}), name)
    assert (rec.language, rec.framework, rec.uiRole, rec.type) == ("jsp", "jsp", role, "code")
    _valid(rec)


def test_a_tag_file_without_jsp_is_not_labelled(tmp_path) -> None:
    # `.tag` is also a Riot.js component: no JSP construct, no `jsp` framework.
    rec = _parse(_repo(tmp_path, {"todo.tag": "<todo><h3>{ opts.title }</h3></todo>"}), "todo.tag")
    assert rec.framework is None


# ── JSP 1.0: the standard syntax ───────────────────────────────────────────────────────────

_JSP10 = """<%@ page import="com.shop.Cart, java.util.*" errorPage="error.jsp" %>
<%@ include file="header.jsp" %>
<%-- a JSP comment, even with %> in it --%>
<%! private int hits = 0;
    public String greet(String who) { return "Hello " + who; } %>
<html><body>
<% java.sql.Connection con = ds.getConnection();
   java.sql.PreparedStatement ps = con.prepareStatement("SELECT id FROM orders");
   for (int i = 0; i < 3; i++) { %>
  <li><%= greet("row") %></li>
<% } %>
<jsp:useBean id="cart" class="com.shop.Cart" scope="session"/>
<jsp:include page="footer.jsp" flush="true"/>
<jsp:forward page="next.jsp"/>
<!-- an HTML comment -->
</body></html>
"""
_SHOP = {
    "web/WEB-INF/web.xml": '<web-app version="2.2"/>',
    "web/index.jsp": _JSP10,
    "web/header.jsp": "h", "web/footer.jsp": "f", "web/next.jsp": "n", "web/error.jsp": "e",
    "src/com/shop/Cart.java": "package com.shop; public class Cart {}",
}


def test_jsp10_directives_comments_and_actions(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _SHOP), "web/index.jsp")
    assert _jsp_names(rec, "directive") == ["page", "include"]
    [jsp_comment] = _jsp(rec, "comment")
    assert jsp_comment.semanticType == "comment" and "%> in it" in jsp_comment.text
    assert [s.semanticType for s in _types(rec, "comment")] == ["comment"]
    assert _names(rec, "element") == ["jsp:useBean", "jsp:include", "jsp:forward"]


def test_jsp10_edges(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _SHOP), "web/index.jsp")
    assert rec.importFiles == ["src/com/shop/Cart.java", "web/error.jsp", "web/header.jsp",
                               "web/footer.jsp", "web/next.jsp"]
    assert rec.externalImports == ["java.util.*"]


def test_jsp10_scripting_is_java(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _SHOP), "web/index.jsp")
    [greet] = rec.functions
    assert (greet.name, greet.startLine, greet.parentId, greet.returnType) == (
        "greet", 5, rec.id, "String")
    assert [p.name for p in greet.params] == ["who"]
    query = next(s for s in rec.statements if s.name == "ps")
    assert query.semanticType == "query_statement" and query.startLine == 8
    assert any(s.nodeType == "for_statement" and s.startLine == 9 for s in rec.statements)
    expr = next(s for s in rec.statements if s.nodeType == "expression_statement")
    assert expr.startLine == 10 and 'greet("row")' in expr.text
    _valid(rec)


def test_capture_off_keeps_structure_and_edges(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _SHOP), "web/index.jsp", capture=False)
    assert rec.statements == []
    assert [f.name for f in rec.functions] == ["greet"]
    assert "web/header.jsp" in rec.importFiles


# ── JSP 1.1: tag libraries ─────────────────────────────────────────────────────────────────

def test_jsp11_taglib_uri_mapped_in_web_xml(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "app/WEB-INF/web.xml": (
            '<!DOCTYPE web-app PUBLIC "-//Sun Microsystems, Inc.//DTD Web Application 2.2//EN" '
            '"http://java.sun.com/j2ee/dtds/web-app_2_2.dtd">\n<web-app><taglib>'
            "<taglib-uri>/shop</taglib-uri><taglib-location>/WEB-INF/shop.tld</taglib-location>"
            "</taglib></web-app>"),
        "app/WEB-INF/shop.tld": "<taglib><shortname>shop</shortname></taglib>",
        "app/list.jsp": '<%@ taglib uri="/shop" prefix="shop" %>\n<shop:items/>\n',
    })
    rec = _parse(repo, "app/list.jsp")
    assert rec.importFiles == ["app/WEB-INF/shop.tld"]
    assert _names(rec, "element") == ["shop:items"]


def test_tld_found_by_its_declared_uri(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "lib/src/META-INF/ui.tld": "<taglib><uri>http://acme.com/ui</uri></taglib>",
        "app/WEB-INF/web.xml": "<web-app/>",
        "app/a.jsp": '<%@ taglib uri="http://acme.com/ui" prefix="ui" %>',
    })
    assert _parse(repo, "app/a.jsp").importFiles == ["lib/src/META-INF/ui.tld"]


# ── JSP 1.2: XML syntax and JSTL 1.0 ───────────────────────────────────────────────────────

_JSP12_XML = """<jsp:root xmlns:jsp="http://java.sun.com/JSP/Page" version="1.2"
          xmlns:c="http://java.sun.com/jstl/core">
  <jsp:directive.page import="java.util.List"/>
  <jsp:directive.include file="part.jspf"/>
  <jsp:declaration>int size(List l) { return l.size(); }</jsp:declaration>
  <jsp:scriptlet>if (a &lt; b &amp;&amp; ok) { save(); }</jsp:scriptlet>
  <jsp:scriptlet><![CDATA[ if (a < b && ok) { load(); } ]]></jsp:scriptlet>
  <jsp:expression>size(items)</jsp:expression>
  <c:import url="part.jspf"/>
</jsp:root>
"""


def test_jsp12_xml_syntax_matches_standard_syntax(tmp_path) -> None:
    repo = _repo(tmp_path, {"doc.jsp": _JSP12_XML, "part.jspf": "p"})
    rec = _parse(repo, "doc.jsp")
    assert _jsp_names(rec, "directive") == ["page", "include"]
    assert [f.name for f in rec.functions] == ["size"]
    ifs = [s.text for s in rec.statements if s.nodeType == "if_statement"]
    assert ifs == ["if (a < b && ok) { save(); }", "if (a < b && ok) { load(); }"]
    assert next(s for s in rec.statements if s.nodeType == "expression_statement"
                and "size(items)" in s.text).startLine == 8
    assert rec.importFiles == ["part.jspf"]  # include directive and c:import, deduplicated
    assert rec.externalImports == ["java.util.List", "http://java.sun.com/jstl/core"]
    assert "jsp:scriptlet" not in _names(rec, "element")


# ── JSP 2.0: EL, tag files, .jspx, jsp-config ──────────────────────────────────────────────

_TAGS = {
    "web/WEB-INF/web.xml": """<web-app version="2.4" xmlns="http://java.sun.com/xml/ns/j2ee">
      <jsp-config><jsp-property-group><url-pattern>*.jsp</url-pattern>
        <include-prelude>/WEB-INF/jspf/top.jspf</include-prelude>
        <include-coda>/WEB-INF/jspf/bottom.jspf</include-coda>
      </jsp-property-group></jsp-config></web-app>""",
    "web/WEB-INF/jspf/top.jspf": '<%@ taglib prefix="ui" tagdir="/WEB-INF/tags/ui" %>',
    "web/WEB-INF/jspf/bottom.jspf": "</body>",
    "web/WEB-INF/tags/ui/card.tag": (
        '<%@ tag body-content="scriptless" %>\n<%@ attribute name="title" required="true" %>\n'
        "<div><h2>${title}</h2><jsp:doBody/></div>\n"),
    "web/WEB-INF/tags/ui/row.tagx": (
        '<jsp:root xmlns:jsp="http://java.sun.com/JSP/Page" version="2.0">'
        '<jsp:directive.tag/><tr>${cells}</tr></jsp:root>'),
    "web/cards.jsp": (
        '<%@ taglib prefix="ui" tagdir="/WEB-INF/tags/ui" %>\n'
        '<ui:card title="${shop.name}">\n  <jsp:attribute name="footer">${shop.phone}</jsp:attribute>\n'
        "</ui:card>\n<ui:row cells=\"${cells}\"/>\n<p>\\${literal} ${a.b}</p>\n"),
}


def test_jsp20_tag_files_and_prelude(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _TAGS), "web/cards.jsp")
    assert rec.importFiles == ["web/WEB-INF/jspf/top.jspf", "web/WEB-INF/jspf/bottom.jspf",
                               "web/WEB-INF/tags/ui/card.tag", "web/WEB-INF/tags/ui/row.tagx"]


def test_jsp20_el_in_template_text(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _TAGS), "web/cards.jsp")
    assert _jsp_names(rec, "el") == ["shop.name", "shop.phone", "cells", "a.b"]
    # `\${literal}` is escaped text, never EL
    assert "literal" not in _jsp_names(rec, "el")


def test_jsp20_tag_file(tmp_path) -> None:
    repo = _repo(tmp_path, _TAGS)
    card = _parse(repo, "web/WEB-INF/tags/ui/card.tag")
    assert card.uiRole == "component"
    assert _jsp_names(card, "directive") == ["tag", "attribute"]
    assert _jsp_names(card, "el") == ["title"]
    row = _parse(repo, "web/WEB-INF/tags/ui/row.tagx")
    assert _jsp_names(row, "el") == ["cells"] and _jsp_names(row, "directive") == ["tag"]


def test_el_is_text_before_servlet_24(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "w/WEB-INF/web.xml": (
            '<!DOCTYPE web-app PUBLIC "-//Sun Microsystems, Inc.//DTD Web Application 2.3//EN" '
            '"http://java.sun.com/dtd/web-app_2_3.dtd"><web-app/>'),
        "w/old.jsp": "<p>${price}</p>",
        "w/on.jsp": '<%@ page isELIgnored="false" %><p>${price}</p>',
    })
    assert _jsp(_parse(repo, "w/old.jsp"), "el") == []
    assert _jsp_names(_parse(repo, "w/on.jsp"), "el") == ["price"]


def test_el_ignored_by_page_or_property_group(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "w/WEB-INF/web.xml": """<web-app version="3.0"><jsp-config><jsp-property-group>
            <url-pattern>/legacy/*</url-pattern><el-ignored>true</el-ignored>
            </jsp-property-group></jsp-config></web-app>""",
        "w/legacy/a.jsp": "${x}",
        "w/b.jsp": '<%@ page isELIgnored="true" %>${x}',
        "w/c.jsp": "${x}",
    })
    assert _jsp(_parse(repo, "w/legacy/a.jsp"), "el") == []
    assert _jsp(_parse(repo, "w/b.jsp"), "el") == []
    assert _jsp_names(_parse(repo, "w/c.jsp"), "el") == ["x"]


# ── JSP 2.1: deferred EL ───────────────────────────────────────────────────────────────────

def test_jsp21_deferred_el(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "w/WEB-INF/web.xml": '<web-app version="2.5"/>',
        "w/a.jsp": "<h:input value=\"#{bean.name}\"/>${now}",
        "w/b.jsp": '<%@ page deferredSyntaxAllowedAsLiteral="true" %>#{bean.name}',
        "v/WEB-INF/web.xml": '<web-app version="2.4"/>',
        "v/c.jsp": "#{bean.name}",
    })
    assert _jsp_names(_parse(repo, "w/a.jsp"), "el") == ["bean.name", "now"]
    assert _jsp(_parse(repo, "w/b.jsp"), "el") == []
    assert _jsp(_parse(repo, "v/c.jsp"), "el") == []  # JSP 2.0: `#{` is text


# ── JSP 2.2 / 2.3: EL method calls and EL 3.0 ──────────────────────────────────────────────

def test_jsp23_el_braces_strings_and_lambdas(tmp_path) -> None:
    page = ("${cart.total(tax)}\n"
            "${[1,2,3].stream().filter(x -> x > 1).toList()}\n"
            "${{'a':1, 'b':2}['a']}\n"
            "${'a } in a string'}\n"
            '${"x" += "}"}\n')
    rec = _parse(_repo(tmp_path, {"a.jsp": page}), "a.jsp")
    assert _jsp_names(rec, "el") == [
        "cart.total(tax)", "[1,2,3].stream().filter(x -> x > 1).toList()",
        "{'a':1, 'b':2}['a']", "'a } in a string'", '"x" += "}"']
    assert [s.startLine for s in _jsp(rec, "el")] == [1, 2, 3, 4, 5]


# ── JSP 3.0 / 3.1: Jakarta ─────────────────────────────────────────────────────────────────

def test_jsp3_jakarta_taglibs(tmp_path) -> None:
    page = ('<%@ page import="jakarta.servlet.http.HttpSession" errorOnELNotFound="true" %>\n'
            '<%@ taglib prefix="c" uri="jakarta.tags.core" %>\n'
            '<%@ taglib prefix="fmt" uri="jakarta.tags.fmt" %>\n'
            '<c:import url="part.jspf"/><c:out value="${x}"/>\n')
    rec = _parse(_repo(tmp_path, {"a.jsp": page, "part.jspf": "p"}), "a.jsp")
    assert rec.importFiles == ["part.jspf"]
    assert rec.externalImports == [
        "jakarta.servlet.http.HttpSession", "jakarta.tags.core", "jakarta.tags.fmt"]


def test_c_import_of_another_context_or_url_is_no_edge(tmp_path) -> None:
    page = ('<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n'
            '<c:import url="/x.jsp" context="/other"/><c:import url="https://cdn.example/x"/>')
    rec = _parse(_repo(tmp_path, {"a.jsp": page, "x.jsp": "x"}), "a.jsp")
    assert rec.importFiles == []


# ── JSP 4.0: removed constructs are tolerated ──────────────────────────────────────────────

def test_jsp40_removed_constructs_are_tolerated(tmp_path) -> None:
    page = ('<%@ page isThreadSafe="false" %>\n'
            '<jsp:plugin type="applet" code="Clock.class" codebase="/applets">\n'
            '  <jsp:params><jsp:param name="tz" value="UTC"/></jsp:params>\n'
            "  <jsp:fallback>No applets</jsp:fallback>\n</jsp:plugin>\n")
    rec = _parse(_repo(tmp_path, {"a.jsp": page}), "a.jsp")
    assert _jsp_names(rec, "directive") == ["page"]
    assert "jsp:plugin" in _names(rec, "element")
    _valid(rec)


# ── encodings ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '<%@ page pageEncoding="ISO-8859-1" %><% String s = "café"; %>'.encode("latin-1"),
    '<%@ page contentType="text/html; charset=windows-1252" %><% String s = "café"; %>'
    .encode("cp1252"),
    '<%@ page pageEncoding="Shift_JIS" %><% String s = "カフェ"; %>'.encode("shift_jis"),
    '<%@ page pageEncoding="UTF-16" %><% String s = "café"; %>'.encode("utf-16"),
    b"\xef\xbb\xbf" + '<%@ page %><% String s = "café"; %>'.encode(),
    '<% String s = "café"; %>'.encode("latin-1"),  # undeclared: falls back, never fails
])
def test_legacy_encodings_decode(tmp_path, raw) -> None:
    rec = _parse(_repo(tmp_path, {"a.jsp": raw}), "a.jsp")
    decl = next(s for s in rec.statements if s.name == "s")
    assert decl.nodeType == "local_variable_declaration"
    assert ("café" in decl.text) or ("カフェ" in decl.text)


def test_decode_keeps_utf8_as_is() -> None:
    raw = '<p>café</p>'.encode()
    assert decode_source(raw) is raw


# ── spec escapes and malformed input ───────────────────────────────────────────────────────

def test_spec_escapes() -> None:
    src = b'<p>&lt; <\\% not code %\\></p><% String s = "50%\\> off"; %>'
    [island] = scan_islands(src)
    assert island.kind == "scriptlet" and src[island.end - 2:island.end] == b"%>"
    assert b"50%\\> off" in src[island.body_start:island.body_end]


@pytest.mark.parametrize("page", [
    "<% if (x) {", "<%= a", "<%-- never closed", "<%@ page import=", "<jsp:scriptlet>a();",
    "${unclosed", "<jsp:directive.page", "<%%>", "%>", "<jsp:expression/>",
])
def test_malformed_pages_never_fail(tmp_path, page) -> None:
    _valid(_parse(_repo(tmp_path, {"a.jsp": page}), "a.jsp"))


def test_without_index_edges_still_resolve_on_disk(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, _SHOP), "web/index.jsp", index=False)
    assert "web/header.jsp" in rec.importFiles
    assert "com.shop.Cart" in rec.externalImports  # no Java index in a single-file parse


def test_context_relative_path_without_web_root_is_no_edge(tmp_path) -> None:
    rec = _parse(_repo(tmp_path, {"a.jsp": '<%@ include file="/b.jsp" %>', "b.jsp": "b"}), "a.jsp")
    assert rec.importFiles == []


# ── registry, template gate, pipeline ──────────────────────────────────────────────────────

def test_selection_and_capabilities() -> None:
    registry.clear()
    registry.register(JspParser())
    for name in ("a.jsp", "a.JSP", "a.jspx", "a.jspf", "a.tag", "a.tagx"):
        assert registry.select(name, b"").name == "jsp"
    caps = registry.capabilities()
    assert {".jsp", ".jspx", ".jspf", ".tag", ".tagx"} <= set(caps["templateExtensions"])
    assert "jsp" in caps["frameworks"] and "synthetic" in caps["statementTypes"]
    registry.clear()


def test_pipeline_gates_jsp_behind_capture_templates(tmp_path) -> None:
    repo = _repo(tmp_path / "r", _SHOP)
    assert not any(r.path.endswith(".jsp") for r in iter_file_records(repo))
    records = {r.path: r for r in iter_file_records(repo, capture_templates=True)}
    page = records["web/index.jsp"]
    assert page.language == "jsp" and "src/com/shop/Cart.java" in page.importFiles
    errors = [v for rec in records.values() for v in validate(rec) if v.severity == "error"]
    assert not errors


# ── navigation (Struts2 tags and links) ────────────────────────────────────────────────────

_NAV = """<%@ taglib prefix="s" uri="/struts-tags" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<s:form action="save" namespace="/orders"/>
<s:form action="save"/>
<c:redirect url="/done.jsp"/>
<jsp:forward page="/next.jsp"/>
<a href="/a.do">a</a><a href="/style.css">css</a><a href="#top">top</a>
"""


def test_navigation_without_index(tmp_path) -> None:
    # Single-file parse: default `.action` extension, no rendering namespace known.
    rec = _parse(_repo(tmp_path, {"a.jsp": _NAV}), "a.jsp", index=False)
    navs = [(s.name, s.endpoint, s.framework) for s in rec.statements
            if s.routeKind == "navigation"]
    assert navs == [("s:form", "/orders/save.action", "struts2"),
                    ("c:redirect", "/done.jsp", "jsp"), ("jsp:forward", "/next.jsp", "jsp"),
                    ("a", "/a.do", "jsp")]  # the namespace-less s:form gives no link
    assert all(s.semanticType == "route" for s in rec.statements if s.routeKind == "navigation")


def test_navigation_is_gated(tmp_path) -> None:
    repo = _repo(tmp_path, {"a.jsp": _NAV, "fixtures/a.jsp": _NAV})
    assert _parse(repo, "a.jsp", capture=False).statements == []
    fixture = _parse(repo, "fixtures/a.jsp")
    assert not any(s.routeKind == "navigation" for s in fixture.statements)
    assert "s:form" in _names(fixture, "element")  # still an element


def test_imported_class_declared_twice_is_external(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "web/a.jsp": '<%@ page import="com.shop.Cart" %>',
        "mod1/src/com/shop/Cart.java": "package com.shop; public class Cart {}",
        "mod2/src/com/shop/Cart.java": "package com.shop; public class Cart {}",
    })
    rec = _parse(repo, "web/a.jsp")
    assert rec.importFiles == [] and rec.externalImports == ["com.shop.Cart"]
