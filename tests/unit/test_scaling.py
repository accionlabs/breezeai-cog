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


SAMPLES: dict[str, tuple[str, Callable[[int], str]]] = {
    "java-vertx": ("V.java", _java_vertx),
    "groovy-vertx": ("Http.groovy", _groovy_vertx),
    "typescript-express-aws": ("app.ts", _ts_express_aws),
    "python": ("mod.py", _python),
    "python-flask": ("app.py", _python_flask),
    "python-django": ("urls.py", _python_django),
    "python-drf": ("urls.py", _python_drf),
    "python-drf-router-subclass": ("urls.py", _python_drf_router_subclass),
    "python-flask-rest": ("api.py", _python_flask_rest),
    "csharp": ("C.cs", _csharp),
    "xsd": ("schema.xsd", _xsd),
    "wsdl": ("service.wsdl", _wsdl),
    "jslt": ("transform.jslt", _jslt),
    "go": ("p.go", _go),
    "go-gin": ("routes.go", _go_gin),
    "c": ("mod.c", _c),
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
