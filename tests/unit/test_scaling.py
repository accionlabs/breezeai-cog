"""Parse cost must stay linear in file size (parser review guide §6.1: a fixed number of walks,
never a scan of all statements per item found).

Per-item scans are invisible on ordinary files and in every behavioural test — they only bite
on very large ones (a 64k-line verticle once took minutes). So this measures *work*, not time:
it counts executed Python lines while parsing a synthetic file and the same file at twice the
size. Linear code does ~2× the lines; a per-item scan does ~4× at these sizes. Lines, not calls:
a scan is often a plain ``for`` loop inside one function, which makes no calls per iteration.
Line counts are deterministic, so the test cannot flake the way a wall-clock check would.

For wall-clock numbers on large files, run ``scripts/bench_parse_scaling.py``."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext

#: Linear code measures exactly 2.00 here (line counts are deterministic); the per-item scans
#: this guards against measured 2.4–3.5 at this size, so 2.2 separates them with margin.
MAX_RATIO = 2.2
SIZE = 200  # methods/routes in the smaller file; the larger one has twice as many


def _java_vertx(n: int) -> str:
    body = "".join(
        f"  // doc for m{i}\n"
        f"  public void m{i}(String s) {{\n"
        f"    int x = {i}; // note\n"
        f'    vertx.eventBus().consumer("a/{i}", msg -> {{ vertx.eventBus().send("b", s); }});\n'
        f"    if (x > 0) {{ foo(x); }}\n"
        f"  }}\n"
        for i in range(n)
    )
    return (
        "package a;\nimport io.vertx.core.AbstractVerticle;\n"
        f"public class V extends AbstractVerticle {{\n{body}}}\n"
    )


def _groovy_vertx(n: int) -> str:
    body = "".join(
        f"    // route {i}\n"
        f"    // second line {i}\n"
        f'    route.get("/r{i}", {{ req ->\n'
        f"      // forward {i}\n"
        f'      vertx.eventBus().send("a/{i}", req.params())  /* async */\n'
        f"    }})\n"
        for i in range(n)
    )
    return (
        "import org.vertx.groovy.core.http.RouteMatcher\n"
        f"class Http {{\n  def routes() {{\n    def route = new RouteMatcher()\n{body}  }}\n}}\n"
    )


def _ts_express_aws(n: int) -> str:
    body = "".join(
        f"// route {i}\n"
        f"// second line {i}\n"
        f"app.get('/r{i}', async (req, res) => {{\n"
        f"  /* publish {i} */\n"
        f"  await sns.send(new PublishCommand({{ TopicArn: 't{i}', Message: 'm' }}));\n"
        f"  if (req.query.x) {{ res.send('a'); }} // note\n"
        f"}});\n"
        for i in range(n)
    )
    return (
        "import express from 'express';\n"
        "import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';\n"
        f"const app = express();\nconst sns = new SNSClient({{}});\n{body}"
    )


def _python(n: int) -> str:
    return "".join(
        f"# helper {i}\n"
        f"def f{i}(x):\n"
        f"    y = x + {i}  # note\n"
        f"    if y > 0:\n"
        f"        return y\n"
        f"    return 0\n\n"
        for i in range(n)
    )


def _csharp(n: int) -> str:
    body = "".join(
        f"    /// <summary>M{i}</summary>\n"
        f"    public int M{i}(int x) {{\n"
        f"        var y = x + {i}; // note\n"
        f"        if (y > 0) {{ return y; }}\n"
        f"        return 0;\n"
        f"    }}\n"
        for i in range(n)
    )
    return f"namespace A;\npublic class C {{\n{body}}}\n"


def _xsd(n: int) -> str:
    body = "".join(
        f'  <!-- field {i} -->\n  <xs:element name="f{i}" type="xs:string"/>\n' for i in range(n)
    )
    return (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        f"{body}"
        "</xs:schema>\n"
    )


def _wsdl(n: int) -> str:
    messages = "".join(
        f'  <wsdl:message name="M{i}Request"/>\n  <wsdl:message name="M{i}Response"/>\n'
        for i in range(n)
    )
    operations = "".join(
        f"    <!-- op {i} -->\n"
        f'    <wsdl:operation name="Op{i}">\n'
        f'      <wsdl:input message="tns:M{i}Request"/>\n'
        f'      <wsdl:output message="tns:M{i}Response"/>\n'
        f"    </wsdl:operation>\n"
        for i in range(n)
    )
    return (
        '<?xml version="1.0"?>\n'
        '<wsdl:definitions xmlns:tns="urn:x" xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">\n'
        f"{messages}"
        '  <wsdl:portType name="Port">\n'
        f"{operations}"
        "  </wsdl:portType>\n"
        "</wsdl:definitions>\n"
    )


def _jslt(n: int) -> str:
    body = "".join(f"// doc for f{i}\ndef f{i}(x)\n  x + {i} // note\n\n" for i in range(n))
    return f"{body}f0(1)\n"


def _go(n: int) -> str:
    body = "".join(
        f"// doc for F{i}\n"
        f'const Path{i} = "/r{i}"\n'
        f"type T{i} struct{{ N int }}\n"
        f"type I{i} interface{{ Get{i}(a, b int) (int, error) }}\n"
        f"func (t *T{i}) F{i}(x int, rest ...string) error {{\n"
        f"\tgo func() {{ http.Get(Path{i}) }}()\n"
        f"\tswitch x {{\n\tcase {i}:\n\t\tx++ // note\n\t}}\n"
        f"\treturn nil\n"
        f"}}\n"
        for i in range(n)
    )
    return f'package p\n\nimport "net/http"\n\n{body}'


def _go_gin(n: int) -> str:
    body = "".join(
        f"\tg{i} := r.Group(\"/g{i}\")\n"
        f"\tg{i}.GET(\"/a\", h.List{i}) // note\n"
        f"\tg{i}.POST(\"/b\", func(c *gin.Context) {{ mount{i}(g{i}) }})\n"
        for i in range(n)
    )
    helpers = "".join(f"func mount{i}(g *gin.RouterGroup) {{ g.PUT(\"/c\", put) }}\n" for i in range(n))
    return (f'package p\n\nimport "github.com/gin-gonic/gin"\n\n'
            f"func routes(r *gin.Engine, h *Handler) {{\n{body}}}\n{helpers}")


def _c(n: int) -> str:
    body = "".join(
        f"/* ops {i} */\n"
        f"#define MAX{i} {i}\n"
        f"struct ops{i} {{ int (*run)(int); int count; }};\n"
        f"static int helper{i}(int x) {{ return x + MAX{i}; }}\n"
        f"int f{i}(struct ops{i} *o, int x) {{\n"
        f"    if (x > 0) {{ return helper{i}(x); }} // note\n"
        f"    return o->run(x);\n"
        f"}}\n"
        for i in range(n)
    )
    return f"#include <stdio.h>\n#include \"local.h\"\n{body}"


def _ts_backbone(n: int) -> str:
    body = "".join(
        f"const M{i} = Backbone.Model.extend({{ urlRoot: '/api/m{i}' }});\n"
        f"const m{i} = new M{i}({{ id: {i} }});\n"
        f"m{i}.fetch();\n"
        f"const R{i} = Backbone.Router.extend({{ routes: {{ 'r{i}/:id': 'h{i}' }}, h{i}() {{}} }});\n"
        for i in range(n)
    )
    return f"import Backbone from 'backbone';\n{body}"
def _jsp(n: int) -> str:
    body = "".join(
        f"<%-- doc for row {i} --%>\n"
        f'<%@ include file="part{i}.jspf" %>\n'
        f"<%! int f{i}(int x) {{ return x + {i}; }} %>\n"
        f'<c:forEach items="${{rows{i}}}" var="r"><t:cell v="${{r.name}}"/></c:forEach>\n'
        f"<% for (int i = 0; i < {i}; i++) {{ %><li><%= f{i}(i) %> ${{x{i}.a}}</li><% }} %>\n"
        f"<!-- note {i} -->\n"
        f'<a href="x{i}.action">l</a><s:form action="a{i}"/><c:url value="/p{i}.jsp"/>\n'
        f'<a href="<s:url action="n{i}"/>">n</a><a href="bare{i}">b</a>\n'
        for i in range(n)
    )
    return (f'<%@ taglib prefix="c" uri="jakarta.tags.core" %>\n'
            f'<%@ taglib prefix="s" uri="/struts-tags" %>\n'
            f'<%@ taglib prefix="t" tagdir="/WEB-INF/tags" %>\n{body}')


def _struts2(n: int) -> str:
    body = "".join(
        f'    @Actions({{@Action("a{i}"), @Action(value = "/abs/b{i}", results = '
        f'{{@Result(location = "v{i}.jsp"), @Result(type = "redirectAction", location = "a{i}")}})}})\n'
        f"    public String m{i}() {{ return SUCCESS; }} // note\n"
        for i in range(n)
    )
    return ("package com.shop.actions.admin;\n"
            "import org.apache.struts2.convention.annotation.*;\n"
            "import com.opensymphony.xwork2.ActionSupport;\n"
            '@Namespace("/admin")\n'
            f"public class OrderAction extends ActionSupport {{\n{body}}}\n")


def _struts2_xml(n: int) -> str:
    packages = "".join(
        f'  <package name="p{i}" namespace="/n{i}" extends="struts-default">\n'
        f'    <action name="a{i}" class="com.shop.A{i}" method="m{i}"><result>/a.jsp</result></action>\n'
        f'    <action name="b{i}_*" class="com.shop.{{1}}Action"/>\n'
        f"  </package>\n"
        for i in range(n)
    )
    return f'<?xml version="1.0"?>\n<struts>\n{packages}</struts>\n'


def _kotlin_ktor(n: int) -> str:
    consts = "".join(f'  const val P{i} = "/p{i}"\n' for i in range(n))
    routes = "".join(f"    get(Paths.P{i}) {{ }}\n    get(TOP{i}) {{ }}\n" for i in range(n))
    tops = "".join(f'const val TOP{i} = "/t{i}"\n' for i in range(n))
    return ("package com.acme\nimport io.ktor.server.routing.*\n"
            f"object Paths {{\n{consts}}}\n{tops}fun Route.r() {{\n{routes}}}\n")


def _php_laravel(n: int) -> str:
    # Receiver tracking: a file-level client, closures capturing it, typed properties and
    # promoted parameters, and chained calls — each walks scopes, so a per-item scan shows.
    body = "".join(
        f"// route {i}\n"
        f"Route::get('/r{i}', function () use ($c) {{ $c->get('https://h{i}.example'); }});\n"
        f"$f{i} = fn($x) => $c->post('https://p{i}.example');\n"
        for i in range(n)
    )
    classes = "".join(
        f"class S{i} {{ public function __construct(private Client $http) {{}}\n"
        f"  public function m{i}() {{ return $this->http->get('https://s{i}.example'); }} }}\n"
        for i in range(n)
    )
    return (
        "<?php\nuse GuzzleHttp\\Client;\nuse Illuminate\\Support\\Facades\\Route;\n"
        f"$c = new Client();\n{body}{classes}"
    )


SAMPLES: dict[str, tuple[str, Callable[[int], str]]] = {
    "java-vertx": ("V.java", _java_vertx),
    "groovy-vertx": ("Http.groovy", _groovy_vertx),
    "typescript-express-aws": ("app.ts", _ts_express_aws),
    "typescript-backbone": ("app.ts", _ts_backbone),
    "python": ("mod.py", _python),
    "csharp": ("C.cs", _csharp),
    "xsd": ("schema.xsd", _xsd),
    "wsdl": ("service.wsdl", _wsdl),
    "jslt": ("transform.jslt", _jslt),
    "go": ("p.go", _go),
    "go-gin": ("routes.go", _go_gin),
    "c": ("mod.c", _c),
    "jsp": ("page.jsp", _jsp),
    "java-struts2": ("OrderAction.java", _struts2),
    "struts2-xml": ("struts.xml", _struts2_xml),
    "kotlin-ktor": ("Routes.kt", _kotlin_ktor),
    "php-laravel": ("routes/web.php", _php_laravel),
}


@pytest.fixture(scope="module", autouse=True)
def _builtin_registry():
    registry.clear()
    registry.discover_builtin()
    yield
    registry.clear()


def _lines(rel: str, source: bytes) -> int:
    """Python lines executed while parsing ``source`` (statement capture on)."""
    parser = registry.select(rel, source)
    ctx = ParseContext(path=rel, abs_path=Path(rel), source=source, repo_root=Path("."),
                       capture_statements=True)
    count = 0

    def trace(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        parser.parse_file(ctx)
    finally:
        sys.settrace(previous)
    return count


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_parse_cost_is_linear_in_file_size(name: str) -> None:
    rel, generate = SAMPLES[name]
    _lines(rel, generate(5).encode())  # warm one-time caches (grammar, detector discovery)
    small = _lines(rel, generate(SIZE).encode())
    large = _lines(rel, generate(2 * SIZE).encode())
    ratio = large / small
    assert ratio < MAX_RATIO, (
        f"{name}: doubling the file multiplied parse work by {ratio:.2f} "
        f"({small} → {large} lines) — something now scans per item found (§6.1)"
    )


def _referrer_walk_lines(n: int) -> int:
    """Python lines executed by the ownership step's rule-2 walk over a chain of ``n`` headers
    each included by the next (``h1.h`` includes ``h0.h`` …, ``main.c`` includes the last) —
    the shape where walking back from every header separately is quadratic."""
    from breezeai_cog.core.ownership import _all_referrer_languages

    headers = [f"h{i}.h" for i in range(n)]
    referrers = {h: {headers[i + 1]} for i, h in enumerate(headers[:-1])}
    referrers[headers[-1]] = {"main.c"}
    count = 0

    def trace(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        langs = _all_referrer_languages(set(headers), referrers, {"main.c": "c"}, {})
    finally:
        sys.settrace(previous)
    assert langs[headers[0]] == {"c"}
    return count


def test_ownership_referrer_walk_is_linear_in_headers() -> None:
    small, large = _referrer_walk_lines(SIZE), _referrer_walk_lines(2 * SIZE)
    assert large / small < MAX_RATIO, f"{small} → {large} lines: the walk is no longer linear"


def _index_lines(n: int) -> int:
    """Python lines executed collecting one Kotlin file's importable constants (index pass)."""
    from breezeai_cog.parsers.kotlin.constants import importable_constants
    from breezeai_cog.parsers.treesitter import parse_source

    source = _kotlin_ktor(n).encode()
    root = parse_source("kotlin", source, 0).root_node
    count = 0

    def trace(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        importable_constants(root, source, "com.acme")
    finally:
        sys.settrace(previous)
    return count


def test_kotlin_constant_index_is_linear_in_file_size() -> None:
    _index_lines(5)
    ratio = _index_lines(2 * SIZE) / _index_lines(SIZE)
    assert ratio < MAX_RATIO, f"kotlin constant index: doubling multiplied work by {ratio:.2f}"
