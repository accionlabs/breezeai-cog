"""Struts2 role table: docs/entry-point-roles/struts2.md — one test per row."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

RES = "src/main/resources"
JAVA = "src/main/java"
POM = {"pom.xml": "<project><dependency><artifactId>struts2-convention-plugin</artifactId>"
                  "</dependency></project>"}

LOGIN = """package com.shop.web;
import com.opensymphony.xwork2.ActionSupport;
public class LoginAction extends ActionSupport {
    public String execute() { return SUCCESS; }
    public String save() { return SUCCESS; }
}
"""


def _xml(body: str, constants: str = "") -> str:
    return f'<?xml version="1.0"?>\n<struts>\n{constants}{body}</struts>\n'


def _eps(stmts, path: str | None = None):
    return [s for s in stmts if is_entry_point(s) and (path is None or s.path == path)]


def test_action_element_is_binding(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml(
            '<package name="p" namespace="/admin" extends="struts-default">\n'
            '  <action name="login" class="com.shop.web.LoginAction" method="save"/>\n'
            "</package>\n"),
        f"{JAVA}/com/shop/web/LoginAction.java": LOGIN,
    })
    [r] = _eps(st, f"{RES}/struts.xml")
    assert (r.semanticType, r.method, r.routeKind, r.framework, r.endpoint) == (
        "route", "ANY", "route", "struts2", "/admin/login.action")
    assert (r.handler, r.handlerPath, r.handlerLine, r.startLine) == (
        "com.shop.web.LoginAction.save", f"{JAVA}/com/shop/web/LoginAction.java", 5, 4)


def test_action_method_defaults_to_execute(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml(
            '<package name="p"><action name="in" class="com.shop.web.LoginAction"/></package>\n'),
        f"{JAVA}/com/shop/web/LoginAction.java": LOGIN,
    })
    [r] = _eps(st, f"{RES}/struts.xml")
    assert (r.endpoint, r.handler, r.handlerLine) == (
        "/in.action", "com.shop.web.LoginAction.execute", 4)


def test_unlocated_class_keeps_handler_as_written(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<package name="p"><action name="a" class="loginBean"/>'
        '<action name="b" class="com.gone.Missing"/></package>\n')})
    a, b = _eps(st)
    assert (a.handler, a.handlerPath, a.handlerLine) == ("loginBean.execute", None, None)
    assert (b.handler, b.handlerPath, b.handlerLine) == ("com.gone.Missing.execute", None, None)


def test_action_without_class_has_no_handler(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<package name="p"><action name="index"><result>/index.jsp</result></action>'
        "</package>\n")})
    [r] = _eps(st)
    assert (r.endpoint, r.handler, r.handlerLine) == ("/index.action", None, None)


def test_wildcard_action_keeps_pattern_without_handler(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<package name="p" namespace="/u">'
        '<action name="user_*" class="com.shop.{1}Action" method="{1}"/></package>\n')})
    [r] = _eps(st)
    assert (r.endpoint, r.handler) == ("/u/user_*.action", None)


@pytest.mark.parametrize("namespace,expected", [
    ("", "/a.action"), ("/", "/a.action"), ("/shop/admin", "/shop/admin/a.action"),
])
def test_package_namespace_is_wiring(analyze_files, namespace, expected) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        f'<package name="p" namespace="{namespace}"><action name="a"/></package>\n')})
    assert [r.endpoint for r in _eps(st)] == [expected]


@pytest.mark.parametrize("files,expected", [
    ({f"{RES}/struts.xml": _xml('<package name="p"><action name="a"/></package>\n',
                                '<constant name="struts.action.extension" value="do,,"/>\n')},
     "/a.do"),
    ({f"{RES}/struts.xml": _xml('<package name="p"><action name="a"/></package>\n',
                                '<constant name="struts.action.extension" value=",action"/>\n')},
     "/a"),
    ({f"{RES}/struts.xml": _xml('<package name="p"><action name="a"/></package>\n'),
      f"{RES}/struts.properties": "struts.action.extension=html\n"}, "/a.html"),
])
def test_extension_constant_is_wiring(analyze_files, files, expected) -> None:
    assert [r.endpoint for r in _eps(analyze_files(files))] == [expected]


def test_include_is_wiring_with_an_edge(analyze_files, tmp_path) -> None:
    from breezeai_cog import iter_file_records

    files = {
        f"{RES}/struts.xml": _xml('<include file="struts-admin.xml"/>\n',
                                  '<constant name="struts.action.extension" value="do"/>\n'),
        f"{RES}/struts-admin.xml": _xml(
            '<package name="a" namespace="/admin"><action name="home"/></package>\n'),
    }
    st = analyze_files(files)
    assert _eps(st, f"{RES}/struts.xml") == []
    [r] = _eps(st, f"{RES}/struts-admin.xml")
    assert r.endpoint == "/admin/home.do"  # the including file's extension
    records = {rec.path: rec for rec in iter_file_records(tmp_path / "repo")}
    assert records[f"{RES}/struts.xml"].importFiles == [f"{RES}/struts-admin.xml"]


ORDERS = """package com.shop.actions;
import org.apache.struts2.convention.annotation.*;
import com.opensymphony.xwork2.ActionSupport;
public class OrderAction extends ActionSupport {
    @Actions({@Action("list"), @Action("/legacy/orders")})
    public String list() { return SUCCESS; }
    @Action(value = "view", results = {@Result(name = "success", location = "view.jsp")})
    public String view() { return SUCCESS; }
    @Action
    public String bare() { return SUCCESS; }
}
"""


def test_action_annotation_on_method_is_binding(analyze_files) -> None:
    st = analyze_files({f"{JAVA}/com/shop/actions/OrderAction.java": ORDERS})
    got = [(r.endpoint, r.handler, r.handlerLine, r.startLine, r.method) for r in _eps(st)]
    assert got == [
        ("/list.action", "list", 5, 5, "ANY"),
        ("/legacy/orders.action", "list", 5, 5, "ANY"),
        ("/view.action", "view", 7, 7, "ANY"),
        (None, "bare", 9, 9, "ANY"),  # no value: no address
    ]
    assert {r.framework for r in _eps(st)} == {"struts2"}


def test_action_annotation_on_class_is_execute(analyze_files) -> None:
    src = """package com.shop.actions;
import org.apache.struts2.convention.annotation.Action;
@Action("home")
public class HomeAction {
    public String execute() { return "success"; }
}
"""
    [r] = _eps(analyze_files({f"{JAVA}/com/shop/actions/HomeAction.java": src}))
    assert (r.endpoint, r.handler, r.handlerLine) == ("/home.action", "execute", 5)


def test_namespace_annotations_are_wiring(analyze_files) -> None:
    files = {
        f"{JAVA}/com/shop/actions/A.java": (
            "package com.shop.actions;\nimport org.apache.struts2.convention.annotation.*;\n"
            '@Namespaces({@Namespace("/x"), @Namespace("/y")})\n'
            'public class A { @Action("a") public String go() { return "s"; } }\n'),
        f"{JAVA}/com/shop/actions/report/package-info.java": (
            '@Namespace("/reports")\npackage com.shop.actions.report;\n'
            "import org.apache.struts2.convention.annotation.Namespace;\n"),
        f"{JAVA}/com/shop/actions/report/B.java": (
            "package com.shop.actions.report;\nimport org.apache.struts2.convention.annotation.*;\n"
            'public class B { @Action("b") public String go() { return "s"; } }\n'),
    }
    assert sorted(r.endpoint for r in _eps(analyze_files(files))) == [
        "/reports/b.action", "/x/a.action", "/y/a.action"]


def test_convention_class_is_binding(analyze_files) -> None:
    files = {
        **POM,
        f"{JAVA}/com/shop/actions/admin/UserProfileAction.java": (
            "package com.shop.actions.admin;\nimport com.opensymphony.xwork2.ActionSupport;\n"
            "public class UserProfileAction extends ActionSupport {\n"
            "    public String execute() { return SUCCESS; }\n}\n"),
        f"{JAVA}/com/shop/struts2/Report.java": (  # no suffix, but an ActionSupport
            "package com.shop.struts2;\nimport org.apache.struts2.ActionSupport;\n"
            "public class Report extends ActionSupport {}\n"),
        f"{JAVA}/com/shop/actions/Base.java": (
            "package com.shop.actions;\nimport com.opensymphony.xwork2.ActionSupport;\n"
            "public abstract class BaseAction extends ActionSupport {}\n"),
        f"{JAVA}/com/shop/web/HelpAction.java": (  # no locator in the package
            "package com.shop.web;\nimport com.opensymphony.xwork2.ActionSupport;\n"
            "public class HelpAction extends ActionSupport {}\n"),
    }
    got = sorted((r.endpoint, r.handler, r.handlerLine) for r in _eps(analyze_files(files)))
    assert got == [("/admin/user-profile.action", "execute", 4),
                   ("/report.action", "execute", None)]


def test_convention_needs_the_plugin(analyze_files) -> None:
    files = {f"{JAVA}/com/shop/actions/UserAction.java": (
        "package com.shop.actions;\nimport com.opensymphony.xwork2.ActionSupport;\n"
        "public class UserAction extends ActionSupport {}\n")}
    assert _eps(analyze_files(files)) == []


def test_method_action_keeps_the_default_mapping(analyze_files) -> None:
    # An @Action on another method: the class's execute is still mapped (inherited here).
    st = analyze_files({**POM, f"{JAVA}/com/shop/actions/OrderAction.java": ORDERS})
    assert ("/order.action", "execute") in {(r.endpoint, r.handler) for r in _eps(st)}


@pytest.mark.parametrize("on_class,on_execute,expected", [
    ('@Action("home")\n', "", ["/home.action"]),  # the class's own @Action, no default too
    ("", '    @Action("start")\n', ["/start.action"]),  # execute annotated: no default
])
def test_annotated_execute_or_class_replaces_the_default_mapping(
        analyze_files, on_class, on_execute, expected) -> None:
    src = (f"package com.shop.actions;\nimport org.apache.struts2.convention.annotation.*;\n"
           f"{on_class}public class HomeAction {{\n{on_execute}"
           f'    public String execute() {{ return "success"; }}\n}}\n')
    st = analyze_files({**POM, f"{JAVA}/com/shop/actions/HomeAction.java": src})
    assert [(r.endpoint, r.handler) for r in _eps(st)] == [(e, "execute") for e in expected]


def test_rest_plugin_controller_is_one_binding_per_method(analyze_files) -> None:
    st = analyze_files({
        **POM,
        f"{RES}/struts.xml": _xml("", '<constant name="struts.mapper.class" value="rest"/>\n'
                                      '<constant name="struts.convention.action.suffix" '
                                      'value="Controller"/>\n'),
        f"{JAVA}/com/shop/actions/OrderController.java": (
            "package com.shop.actions;\nimport org.apache.struts2.rest.RestActionSupport;\n"
            "public class OrderController extends RestActionSupport {\n"
            "    public String index() { return null; }\n"
            "    public String show() { return null; }\n"
            "    public String create() { return null; }\n"
            "    public String destroy() { return null; }\n"
            "    public String helper(int x) { return null; }\n}\n"),
    })
    got = [(r.method, r.endpoint, r.handler, r.handlerLine) for r in _eps(st)]
    assert got == [("GET", "/order", "index", 4), ("GET", "/order/{id}", "show", 5),
                   ("POST", "/order", "create", 6), ("DELETE", "/order/{id}", "destroy", 7)]


def test_settings_are_per_module(analyze_files) -> None:
    files = {}
    for module, ext in (("a", "do"), ("b", "html")):
        files[f"{module}/pom.xml"] = POM["pom.xml"]
        files[f"{module}/{RES}/struts.xml"] = _xml(
            "", f'<constant name="struts.action.extension" value="{ext}"/>\n')
        files[f"{module}/{JAVA}/com/x/actions/HomeAction.java"] = (
            "package com.x.actions;\nimport com.opensymphony.xwork2.ActionSupport;\n"
            "public class HomeAction extends ActionSupport {}\n")
    assert sorted(r.endpoint for r in _eps(analyze_files(files))) == ["/home.do", "/home.html"]


@pytest.mark.xfail(strict=True, reason="interceptor stacks are not recorded as guards yet")
def test_interceptor_ref_is_guard(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<package name="p"><action name="a"><interceptor-ref name="authStack"/></action>'
        "</package>\n")})
    [r] = _eps(st)
    assert r.guards == ["authStack"]


WEB = "src/main/webapp"


def _navs(stmts, path: str | None = None):
    return [(s.name, s.endpoint, s.framework) for s in stmts
            if s.routeKind == "navigation" and (path is None or s.path == path)]


def test_result_is_navigation(analyze_files, tmp_path) -> None:
    from breezeai_cog import iter_file_records

    files = {
        f"{WEB}/WEB-INF/jsp/login.jsp": "x",
        f"{WEB}/WEB-INF/jsp/error.jsp": "x",
        f"{RES}/struts.xml": _xml(
            '<package name="p" namespace="/shop">\n'
            '<global-results><result name="error">/WEB-INF/jsp/error.jsp</result></global-results>\n'
            '<action name="a">\n'
            "  <result>/WEB-INF/jsp/login.jsp</result>\n"
            '  <result name="rel">rel.jsp</result>\n'
            '  <result name="next" type="redirectAction">home</result>\n'
            '  <result name="far" type="chain"><param name="actionName">list</param>'
            '<param name="namespace">/admin</param></result>\n'
            '  <result name="away" type="redirect">/logout.action</result>\n'
            '  <result name="web" type="redirect">https://example.com/</result>\n'
            '  <result name="t" type="tiles">home.page</result>\n'
            '  <result name="j" type="json"/>\n'
            "</action></package>\n"),
    }
    st = analyze_files(files)
    assert len(_eps(st)) == 1  # results are not entry points
    assert _navs(st) == [
        ("error", "/WEB-INF/jsp/error.jsp", "struts2"),
        ("success", "/WEB-INF/jsp/login.jsp", "struts2"),
        ("rel", "/shop/rel.jsp", "struts2"),
        ("next", "/shop/home.action", "struts2"),
        ("far", "/admin/list.action", "struts2"),
        ("away", "/logout.action", "struts2"),
    ]
    records = {r.path: r for r in iter_file_records(tmp_path / "repo", capture_templates=True)}
    assert records[f"{RES}/struts.xml"].importFiles == [
        f"{WEB}/WEB-INF/jsp/error.jsp", f"{WEB}/WEB-INF/jsp/login.jsp"]
    # Without --capture-templates the views are not in the graph, so the edges are pruned.
    gated = {r.path: r for r in iter_file_records(tmp_path / "repo")}
    assert gated[f"{RES}/struts.xml"].importFiles == []


def test_tiles_package_results_are_none(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<package name="p" extends="tiles-default"><action name="a">'
        "<result>home.page</result></action></package>\n")})
    assert _navs(st) == []


def test_result_annotations_are_navigation(analyze_files) -> None:
    src = """package com.shop.actions.admin;
import org.apache.struts2.convention.annotation.*;
@Results({@Result(name = "error", location = "/WEB-INF/jsp/error.jsp")})
public class ReportAction {
    @Action(value = "report", results = {@Result(name = "success", location = "report-view.jsp"),
        @Result(name = "back", type = "redirectAction", location = "orders")})
    public String execute() { return "success"; }
}
"""
    st = analyze_files({f"{JAVA}/com/shop/actions/admin/ReportAction.java": src})
    assert _navs(st) == [
        ("success", "/WEB-INF/content/admin/report-view.jsp", "struts2"),
        ("back", "/admin/orders.action", "struts2"),
        ("error", "/WEB-INF/jsp/error.jsp", "struts2"),
    ]
    assert [s.startLine for s in st if s.routeKind == "navigation"] == [5, 6, 3]


def test_relative_redirect_result_resolves_against_the_namespace(analyze_files) -> None:
    # Regression: a redirect is reached from the action's URL, never the Convention view folder.
    src = """package com.shop.actions.admin;
import org.apache.struts2.convention.annotation.*;
public class PersonAction {
    @Action(value = "delete", results = {@Result(name = "success", location = "list", type = "redirect"),
        @Result(name = "input", location = "form.jsp")})
    public String execute() { return "success"; }
}
"""
    st = analyze_files({f"{JAVA}/com/shop/actions/admin/PersonAction.java": src})
    assert _navs(st) == [("success", "/admin/list", "struts2"),
                         ("input", "/WEB-INF/content/admin/form.jsp", "struts2")]


def test_convention_implicit_results(analyze_files) -> None:
    st = analyze_files({
        **POM,
        f"{JAVA}/com/shop/actions/admin/OrdersAction.java": (
            "package com.shop.actions.admin;\nimport com.opensymphony.xwork2.ActionSupport;\n"
            "public class OrdersAction extends ActionSupport {}\n"),
        f"{WEB}/WEB-INF/content/admin/orders.jsp": "x",
        f"{WEB}/WEB-INF/content/admin/orders-error.ftl": "x",
        f"{WEB}/WEB-INF/content/admin/orders.css": "x",
        f"{WEB}/WEB-INF/content/admin/ordersx.jsp": "x",
    })
    assert sorted(_navs(st), key=str) == [
        ("error", "/WEB-INF/content/admin/orders-error.ftl", "struts2"),
        (None, "/WEB-INF/content/admin/orders.jsp", "struts2"),
    ]


LOGIN_JSP = """<%@ taglib prefix="s" uri="/struts-tags" %>
<s:form action="login"><s:submit action="login!cancel"/></s:form>
<s:url action="orders" namespace="/admin"/>
<s:a action="%{next}">dyn</s:a>
<a href="${pageContext.request.contextPath}/shop/help.action">help</a>
<a href="<%= request.getContextPath() %>/shop/faq.do">faq</a>
<a href="other.jsp">relative</a>
<a href="https://x.org/a.jsp">external</a>
<a href="/img/logo.png">image</a>
<a href="${next}.action">dynamic</a>
"""


def test_jsp_struts_tags_and_links_are_navigation(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml(
            '<package name="p" namespace="/shop"><action name="login">'
            "<result>/WEB-INF/jsp/login.jsp</result></action></package>\n"),
        f"{WEB}/WEB-INF/jsp/login.jsp": LOGIN_JSP,
    }, templates=True)
    assert _navs(st, f"{WEB}/WEB-INF/jsp/login.jsp") == [
        ("s:form", "/shop/login.action", "struts2"),
        ("s:submit", "/shop/login.action", "struts2"),
        ("s:url", "/admin/orders.action", "struts2"),
        ("a", "/shop/help.action", "jsp"),
        ("a", "/shop/faq.do", "jsp"),
        ("a", "/shop/other.jsp", "jsp"),  # relative to the rendering action's namespace
    ]  # the dynamic s:a and ${next}.action give no record, never a null endpoint


def test_jsp_page_with_no_single_rendering_namespace(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml(
            '<package name="a" namespace="/a"><action name="x"><result>/WEB-INF/v.jsp</result>'
            '</action></package><package name="b" namespace="/b"><action name="y">'
            "<result>/WEB-INF/v.jsp</result></action></package>\n"),
        f"{WEB}/WEB-INF/v.jsp": ('<%@ taglib prefix="s" uri="/struts-tags" %>'
                                 '<s:form action="save"/><a href="x.action">x</a>'),
    }, templates=True)
    assert _navs(st, f"{WEB}/WEB-INF/v.jsp") == []  # no single namespace: no link at all
    assert [s.name for s in st if s.path == f"{WEB}/WEB-INF/v.jsp" and s.nodeType == "element"] \
        == ["s:form"]  # the tag itself is still captured


def test_directly_served_page_links_resolve_against_its_url(analyze_files) -> None:
    st = analyze_files({
        f"{WEB}/WEB-INF/web.xml": "<web-app/>",
        f"{WEB}/shop/index.jsp": '<a href="sub/b.jsp">b</a><form action="../login.action"></form>',
    }, templates=True)
    assert _navs(st) == [("a", "/shop/sub/b.jsp", "jsp"), ("form", "/login.action", "jsp")]


def test_dynamic_method_invocation_is_none(analyze_files) -> None:
    st = analyze_files({f"{RES}/struts.xml": _xml(
        '<constant name="struts.enable.DynamicMethodInvocation" value="true"/>\n'
        '<package name="p"><action name="a"><allowed-methods>save,delete</allowed-methods>'
        "</action></package>\n")})
    assert [r.endpoint for r in _eps(st)] == ["/a.action"]


def test_struts1_config_is_none(analyze_files) -> None:
    st = analyze_files({"WEB-INF/struts-config.xml": (
        '<struts-config><action-mappings><action path="/login" type="a.LoginAction"/>'
        "</action-mappings></struts-config>\n")})
    assert _eps(st) == []


def test_jsp_struts_tags_use_their_module_extension(analyze_files) -> None:
    # Regression: modules with different extensions must not blank each other's addresses.
    files = {}
    for module, ext in (("a", "do"), ("b", ",,json,action")):
        files[f"{module}/{RES}/struts.xml"] = _xml(
            '<package name="p" namespace="/m"><action name="go">'
            "<result>/WEB-INF/go.jsp</result></action></package>\n",
            f'<constant name="struts.action.extension" value="{ext}"/>\n')
        files[f"{module}/{WEB}/WEB-INF/go.jsp"] = (
            '<%@ taglib prefix="s" uri="/struts-tags" %><s:form action="go"/>')
    navs = _navs(analyze_files(files, templates=True))
    assert sorted(endpoint for name, endpoint, _ in navs if name == "s:form") == [
        "/m/go", "/m/go.do"]


# ── Constant precedence: struts.xml < struts.properties < web.xml init-param ───────────────

_FILTER = """<web-app xmlns="https://jakarta.ee/xml/ns/jakartaee" version="6.0">
  <filter><filter-name>struts2</filter-name>
    <filter-class>org.apache.struts2.dispatcher.filter.StrutsPrepareAndExecuteFilter</filter-class>
    <init-param><param-name>struts.action.extension</param-name><param-value>html</param-value>
    </init-param></filter></web-app>
"""


def _precedence_repo(web_xml: bool, properties: bool) -> dict[str, str]:
    files = {
        f"{RES}/struts.xml": _xml(
            '<package name="p"><action name="go"><result>/WEB-INF/go.jsp</result></action>'
            "</package>\n", '<constant name="struts.action.extension" value="do"/>\n'),
        f"{JAVA}/com/shop/actions/HomeAction.java": (
            "package com.shop.actions;\nimport org.apache.struts2.convention.annotation.Action;\n"
            'public class HomeAction { @Action("home") public String execute() { return ""; } }\n'),
        f"{WEB}/WEB-INF/go.jsp": '<%@ taglib prefix="s" uri="/struts-tags" %><s:form action="go"/>',
    }
    if properties:
        files[f"{RES}/struts.properties"] = "struts.action.extension=jspa\n"
    if web_xml:
        files[f"{WEB}/WEB-INF/web.xml"] = _FILTER
    return files


@pytest.mark.parametrize("web_xml,properties,suffix", [
    (False, False, ".do"),     # the <constant>
    (False, True, ".jspa"),    # struts.properties overrides it
    (True, True, ".html"),     # the filter init-param overrides both
])
def test_extension_precedence_on_every_side(analyze_files, web_xml, properties, suffix) -> None:
    st = analyze_files(_precedence_repo(web_xml, properties), templates=True)
    xml_route = next(r.endpoint for r in _eps(st, f"{RES}/struts.xml"))
    java_route = next(r.endpoint for r in _eps(st) if r.path.endswith("HomeAction.java"))
    jsp_link = next(e for n, e, _ in _navs(st) if n == "s:form")
    assert (xml_route, java_route, jsp_link) == (f"/go{suffix}", f"/home{suffix}", f"/go{suffix}")


def test_web_xml_init_param_of_another_filter_is_ignored(analyze_files) -> None:
    files = _precedence_repo(web_xml=False, properties=False)
    files[f"{WEB}/WEB-INF/web.xml"] = _FILTER.replace(
        "org.apache.struts2.dispatcher.filter.StrutsPrepareAndExecuteFilter", "com.acme.Gzip")
    assert next(r.endpoint for r in _eps(analyze_files(files), f"{RES}/struts.xml")) == "/go.do"


# ── Cross-file data reaches the workers ───────────────────────────────────────────────────

@pytest.mark.parametrize("jobs", [1, 2])
def test_same_records_for_any_jobs(tmp_path, jobs) -> None:
    import gzip
    import json

    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    files = {**_precedence_repo(web_xml=True, properties=False), **POM,
             f"{JAVA}/com/shop/actions/admin/UserProfileAction.java": (
                 "package com.shop.actions.admin;\nimport com.opensymphony.xwork2.ActionSupport;\n"
                 "public class UserProfileAction extends ActionSupport {}\n"),
             f"{JAVA}/com/shop/actions/report/package-info.java": (
                 '@Namespace("/reports")\npackage com.shop.actions.report;\n'
                 "import org.apache.struts2.convention.annotation.Namespace;\n"),
             f"{JAVA}/com/shop/actions/report/SalesAction.java": (
                 "package com.shop.actions.report;\n"
                 "import org.apache.struts2.convention.annotation.Action;\n"
                 'public class SalesAction { @Action("sales") public String go() { return ""; } }\n')}
    for rel, src in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_templates=True, out=tmp_path / "out", jobs=jobs)
    records = [json.loads(line) for line in
               gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()[1:]]
    got = sorted((s.get("routeKind"), s.get("endpoint")) for r in records
                 for s in r.get("statements", [])
                 if s.get("framework") in ("struts2", "jsp") and s.get("semanticType") == "route")
    assert got == [
        ("navigation", "/WEB-INF/go.jsp"),          # the go action's result
        ("navigation", "/go.html"),                 # the JSP's s:form, web.xml extension
        ("route", "/admin/user-profile.html"),      # Convention plugin (from pom.xml)
        ("route", "/go.html"),                      # struts.xml action
        ("route", "/home.html"),                    # @Action
        ("route", "/reports/sales.html"),           # package-info.java namespace
    ]


# ── Negative cases ────────────────────────────────────────────────────────────────────────

def test_action_annotation_of_another_framework_is_none(analyze_files) -> None:
    # Stripes (and others) also have an @Action; without the Convention import it is not ours.
    src = ("package com.shop.actions;\nimport net.sourceforge.stripes.action.*;\n"
           "import com.opensymphony.xwork2.ActionSupport;\n"
           'public class Old extends ActionSupport { @Action("x") public String go() '
           '{ return ""; } }\n')
    assert _eps(analyze_files({f"{JAVA}/com/shop/web/Old.java": src})) == []


def test_class_in_two_source_roots_is_not_located(analyze_files) -> None:
    # The configuration sits at the repo root, so `src/main/java` and `src` are both one step
    # down: two candidates at the nearest level is ambiguous, not a pick.
    st = analyze_files({
        "struts.xml": _xml('<package name="p"><action name="in" '
                           'class="com.shop.web.LoginAction"/></package>\n'),
        f"{JAVA}/com/shop/web/LoginAction.java": LOGIN,
        "src/com/shop/web/LoginAction.java": LOGIN,
    })
    [r] = _eps(st)
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "com.shop.web.LoginAction.execute", None, None)


# ── Extension-less addresses (Struts' default `action,,`), default-namespace fallback ─────

_BARE = {
    f"{RES}/struts.xml": _xml(
        '<package name="p"><action name="bookList" class="x.Books" method="list">'
        "<result>/bookList.jsp</result></action>\n"
        '<action name="saveBook" class="x.Books" method="save">'
        '<result type="redirect">bookList</result></action>\n'
        '<action name="*Book" class="x.Books" method="{1}"/></package>\n'),
    f"{WEB}/WEB-INF/web.xml": "<web-app/>",
    f"{WEB}/bookList.jsp": ('<form action="saveBook" method="post"></form>'
                            '<a href="bookList">list</a><a href="editBook?id=${b.id}">edit</a>'
                            '<a href="unknownThing">x</a><a href="css/site">y</a>'),
}


def test_extension_less_links_and_redirects_name_known_actions(analyze_files) -> None:
    st = analyze_files(_BARE, templates=True)
    assert _navs(st, f"{WEB}/bookList.jsp") == [
        ("form", "/saveBook.action", "jsp"),
        ("a", "/bookList.action", "jsp"),
        ("a", "/editBook.action", "jsp"),  # served by the `*Book` wildcard action
    ]  # `unknownThing` and `css/site` name no action: no link
    redirect = [e for n, e, _ in _navs(st, f"{RES}/struts.xml") if n == "success" and "book" in e.lower()]
    assert "/bookList.action" in redirect  # `<result type="redirect">bookList` pairs now


def test_extension_less_links_need_an_extension_list_allowing_it(analyze_files) -> None:
    files = dict(_BARE)
    files[f"{RES}/struts.xml"] = files[f"{RES}/struts.xml"].replace(
        "<struts>\n", '<struts>\n<constant name="struts.action.extension" value="do"/>\n')
    assert _navs(analyze_files(files, templates=True), f"{WEB}/bookList.jsp") == []


def test_default_namespace_answers_any_namespace(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml(
            '<package name="a"><action name="listrequest"/></package>\n'
            '<package name="b" namespace="/admin"><action name="users"/>'
            "<action name=\"listrequest\"/></package>\n"
            '<package name="c" namespace="/x"><action name="home">'
            "<result>/WEB-INF/home.jsp</result></action></package>\n"),
        f"{WEB}/WEB-INF/home.jsp": (
            '<%@ taglib prefix="s" uri="/struts-tags" %>'
            '<s:url action="listrequest" namespace="/datatables"/>'  # no such namespace: default
            '<s:url action="listrequest" namespace="/admin"/>'       # declared there: kept
            '<s:url action="users" namespace="/nowhere"/>'),         # not in default: as written
    }, templates=True)
    assert [e for n, e, _ in _navs(st, f"{WEB}/WEB-INF/home.jsp")] == [
        "/listrequest.action", "/admin/listrequest.action", "/nowhere/users.action"]


# ── URL tags inside an attribute ──────────────────────────────────────────────────────────

def test_url_tag_inside_an_attribute_is_navigation(analyze_files) -> None:
    st = analyze_files({
        f"{RES}/struts.xml": _xml('<package name="p" namespace="/p"><action name="home">'
                                  "<result>/WEB-INF/home.jsp</result></action></package>\n"),
        f"{WEB}/WEB-INF/home.jsp": (
            '<%@ taglib prefix="s" uri="/struts-tags" %>\n'
            '<%@ taglib prefix="c" uri="jakarta.tags.core" %>\n'
            '<a href="<s:url action="list"/>">list</a>\n'
            "<a href='<c:url value=\"/help.jsp\"/>'>help</a>\n"
            '<s:url action="edit" var="u"/>\n'           # a real element: recorded once
            '<a href="<x:url action="nope"/>">x</a>\n'),  # unbound prefix: nothing
    }, templates=True)
    got = [(n, e, s.startLine) for s in st if s.routeKind == "navigation"
           and s.path == f"{WEB}/WEB-INF/home.jsp" for n, e in [(s.name, s.endpoint)]]
    assert sorted(got) == [("c:url", "/help.jsp", 4), ("s:url", "/p/edit.action", 5),
                           ("s:url", "/p/list.action", 3)]
    # A tag found inside an attribute has no grammar node; the real element keeps its own.
    kinds = {s.startLine: s.nodeType for s in st if s.routeKind == "navigation"
             and s.path == f"{WEB}/WEB-INF/home.jsp"}
    assert kinds == {3: "synthetic", 4: "synthetic", 5: "element"}
