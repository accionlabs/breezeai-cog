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


def _ruby(n: int) -> str:
    body = "".join(
        f"  # doc for m{i}\n"
        f"  def m{i}(user)\n"
        f"    user = User.find({i})\n"
        f"    user.update(name: \"user-{i}\")\n"
        f"    if user.active?\n"
        f"      audit.record(user)\n"
        f"    end\n"
        f"  end\n"
        for i in range(n)
    )
    return f"class UserService\n{body}end\n"


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


SAMPLES: dict[str, tuple[str, Callable[[int], str]]] = {
    "java-vertx": ("V.java", _java_vertx),
    "groovy-vertx": ("Http.groovy", _groovy_vertx),
    "typescript-express-aws": ("app.ts", _ts_express_aws),
    "python": ("mod.py", _python),
    "ruby": ("service.rb", _ruby),
    "csharp": ("C.cs", _csharp),
    "xsd": ("schema.xsd", _xsd),
    "wsdl": ("service.wsdl", _wsdl),
    "jslt": ("transform.jslt", _jslt),
    "go": ("p.go", _go),
    "go-gin": ("routes.go", _go_gin),
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
