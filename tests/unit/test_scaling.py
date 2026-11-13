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


def _python_flask(n: int) -> str:
    body = "".join(
        f"@bp.route('/items/{i}', methods=['GET', 'POST'])\n@login_required\n"
        f"def item{i}():\n    return ''\n\n"
        f"app.add_url_rule('/rule/{i}', view_func=item{i})\n"
        for i in range(n)
    )
    return ("from flask import Flask, Blueprint\nfrom flask_login import login_required\n"
            "app = Flask(__name__)\nbp = Blueprint('b', __name__, url_prefix='/b')\n"
            f"app.register_blueprint(bp)\n{body}")


def _python_flask_views(n: int) -> str:
    """``view = Cls.as_view(...)`` variables, each named by an ``add_url_rule``."""
    body = "".join(
        f"class V{i}(MethodView):\n    def get(self):\n        return ''\n"
        f"v{i} = V{i}.as_view('v{i}')\napp.add_url_rule('/v/{i}', view_func=v{i})\n"
        for i in range(n)
    )
    return f"from flask import Flask\nfrom flask.views import MethodView\napp = Flask(__name__)\n{body}"


def _python_django_method_decorators(n: int) -> str:
    """Class-level ``method_decorator``s on views built on a chain of bases."""
    views = "".join(
        f"@method_decorator(login_required, name='get')\nclass V{i}(B{i}):\n    pass\n"
        f"class B{i}(ListView):\n    def get(self, request):\n        pass\n"
        for i in range(n)
    )
    patterns = "".join(f"    path('v{i}/', V{i}.as_view()),\n" for i in range(n))
    return ("from django.urls import path\nfrom django.views.generic import ListView\n"
            "from django.utils.decorators import method_decorator\n"
            "from django.contrib.auth.decorators import login_required\n"
            f"{views}urlpatterns = [\n{patterns}]\n")


def _python_django(n: int) -> str:
    views = "".join(
        f"class V{i}(LoginRequiredMixin, ListView):\n    def post(self, request):\n        pass\n"
        f"@login_required\ndef f{i}(request):\n    pass\n"
        for i in range(n)
    )
    patterns = "".join(f"    path('v{i}/', V{i}.as_view()),\n    path('f{i}/', f{i}),\n" for i in range(n))
    return ("from django.urls import path\nfrom django.views.generic import ListView\n"
            "from django.contrib.auth.mixins import LoginRequiredMixin\n"
            "from django.contrib.auth.decorators import login_required\n"
            f"{views}urlpatterns = [\n{patterns}]\n")


def _python_drf(n: int) -> str:
    views = "".join(
        f"class S{i}(viewsets.ModelViewSet):\n    serializer_class = Ser{i}\n"
        f"    @action(detail=True, methods=['post'])\n    def go(self, request, pk=None):\n        pass\n"
        for i in range(n)
    )
    registers = "".join(f"router.register('s{i}', S{i})\n" for i in range(n))
    return ("from rest_framework import viewsets\nfrom rest_framework.decorators import action\n"
            "from rest_framework.routers import DefaultRouter\nrouter = DefaultRouter()\n"
            f"{views}{registers}urlpatterns = router.urls\n")


def _python_drf_router_subclass(n: int) -> str:
    return _python_drf(n).replace(
        "router = DefaultRouter()\n",
        "class ApiRouter(DefaultRouter):\n    def __init__(self):\n        super().__init__()\n"
        "        self.routes[0].mapping.update({'put': 'bulk_update'})\nrouter = ApiRouter()\n")


def _python_flask_rest(n: int) -> str:
    body = "".join(
        f"@ns.route('/r{i}/<id>')\nclass R{i}(Resource):\n    def get(self, id):\n        pass\n"
        f"    def put(self, id):\n        pass\n"
        f"api.add_resource(R{i}, '/x{i}', '/y{i}')\n"
        f"@blp.route('/m{i}')\nclass M{i}(MethodView):\n    def post(self):\n        pass\n"
        for i in range(n)
    )
    return ("from flask import Flask\nfrom flask.views import MethodView\n"
            "from flask_restx import Api, Resource\nfrom flask_smorest import Api as SApi, Blueprint\n"
            "app = Flask(__name__)\napi = Api(app, prefix='/v1')\nns = api.namespace('things')\n"
            "blp = Blueprint('b', __name__, url_prefix='/b')\nsapi = SApi(app)\nsapi.register_blueprint(blp)\n"
            f"{body}")


def _python_channels(n: int) -> str:
    consumers = "".join(f"class C{i}(AsyncWebsocketConsumer):\n    async def connect(self):\n        pass\n"
                        for i in range(n))
    patterns = "".join(f"    re_path(r'ws/c{i}/(?P<room>\\w+)/$', C{i}.as_asgi()),\n    path('p{i}/', C{i}),\n"
                       for i in range(n))
    return ("from django.urls import path, re_path\n"
            "from channels.routing import ProtocolTypeRouter, URLRouter\n"
            "from channels.generic.websocket import AsyncWebsocketConsumer\n"
            f"{consumers}websocket_urlpatterns = [\n{patterns}]\n"
            "application = ProtocolTypeRouter({'websocket': URLRouter(websocket_urlpatterns)})\n")


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


def _markdown(n: int) -> str:
    body = "".join(
        f"## Step {i}\n"
        f"Run it with `TOKEN_{i}` set.\n"
        f"```yaml\n- name: DB_PASSWORD\n  value: s{i}\npassword: p{i}\n```\n"
        f"export API_KEY_{i}=k{i}\n"
        for i in range(n)
    )
    return f"---\nname: guide\ndescription: A guide\napi_token: t\n---\n# Guide\n{body}"


def _python_mcp(n: int) -> str:
    tools = "".join(
        f"@mcp.tool(name='t{i}')\n"
        f"def t{i}(q: str, n: int = {i}, ctx: Context = None) -> str:\n"
        f"    \"\"\"Tool {i}.\n\n    Args:\n        q: The query,\n            continued.\n"
        f"        n (int): A number.\n    \"\"\"\n"
        f"    return q\n\n\n"
        for i in range(n)
    )
    return f"from fastmcp import FastMCP, Context\n\nmcp = FastMCP('demo')\n\n\n{tools}"


def _typescript_mcp(n: int) -> str:
    tools = "".join(
        f"server.tool('t{i}', 'Tool {i}', {{ q: z.string().describe('q'), n: z.number().optional() }},\n"
        f"  async ({{ q }}) => ({{ content: [] }}));\n"
        for i in range(n)
    )
    return (
        "import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';\n"
        "import { z } from 'zod';\n"
        f"const server = new McpServer({{ name: 'demo', version: '1' }});\n{tools}"
    )


SAMPLES: dict[str, tuple[str, Callable[[int], str]]] = {
    "java-vertx": ("V.java", _java_vertx),
    "groovy-vertx": ("Http.groovy", _groovy_vertx),
    "typescript-express-aws": ("app.ts", _ts_express_aws),
    "typescript-backbone": ("app.ts", _ts_backbone),
    "python": ("mod.py", _python),
    "python-flask": ("app.py", _python_flask),
    "python-django": ("urls.py", _python_django),
    "python-django-method-decorators": ("urls.py", _python_django_method_decorators),
    "python-flask-views": ("app.py", _python_flask_views),
    "python-drf": ("urls.py", _python_drf),
    "python-drf-router-subclass": ("urls.py", _python_drf_router_subclass),
    "python-flask-rest": ("api.py", _python_flask_rest),
    "python-channels": ("routing.py", _python_channels),
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
    "markdown": ("docs/guide.md", _markdown),
    "python-mcp": ("server.py", _python_mcp),
    "typescript-mcp": ("server.ts", _typescript_mcp),
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
