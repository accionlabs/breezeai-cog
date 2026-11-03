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


def _ts_express_locals(n: int) -> str:
    """Routes built from names inside one function: each name is looked up as a local or a
    constant of the file (``typescript/scopes.py``), never by rescanning the function per name."""
    body = "".join(
        f"  const p{i} = read({i});\n"
        f"  app.get(p{i} + '/r{i}', h);\n"
        f"  app.get(`${{BASE}}/x{i}`, h);\n"
        for i in range(n)
    )
    return (
        "const express = require('express');\nconst app = express();\n"
        f"const BASE = '/api';\nfunction mount() {{\n{body}}}\nfunction h(req, res) {{}}\n"
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
        f"Run it with `TOKEN_{i}` set, or `DB_PASSWORD_{i}=p{i}`.\n"
        f"| Key | Value |\n|---|---|\n| API_KEY_{i} | k{i} |\n"
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


def _python_mcp_lowlevel(n: int) -> str:
    tools = "".join(
        f"        types.Tool(name='t{i}', description='Tool {i}',\n"
        f"                   inputSchema={{'type': 'object', 'properties': {{'q': {{'type': 'string'}}}}}}),\n"
        for i in range(n)
    )
    return (
        "from mcp.server.lowlevel import Server\nimport mcp.types as types\n\n"
        "server = Server('demo')\n\n\n@server.list_tools()\nasync def list_tools():\n"
        f"    return [\n{tools}    ]\n\n\n@server.call_tool()\nasync def call_tool(name, arguments):\n"
        "    return []\n"
    )


def _typescript_mcp_lowlevel(n: int) -> str:
    consts = "".join(
        f"const T{i}: Tool = {{ name: 't{i}', description: 'Tool ' + '{i}', "
        f"inputSchema: {{ type: 'object', properties: {{ q: {{ type: 'string' }} }} }} }};\n"
        for i in range(n)
    )
    refs = ", ".join(f"T{i}" for i in range(n))
    return (
        "import { Server } from '@modelcontextprotocol/sdk/server/index.js';\n"
        "import { CallToolRequestSchema, ListToolsRequestSchema, Tool } from "
        "'@modelcontextprotocol/sdk/types.js';\n"
        f"{consts}const server = new Server({{ name: 'demo', version: '1' }}, {{}});\n"
        f"server.setRequestHandler(ListToolsRequestSchema, async () => ({{ tools: [{refs}] }}));\n"
        "server.setRequestHandler(CallToolRequestSchema, async () => ({ content: [] }));\n"
    )


def _go_mcp(n: int) -> str:
    types = "".join(
        f"type Args{i} struct {{\n\tName string `json:\"name\" jsonschema:\"who\"`\n"
        f"\tN int `json:\"n,omitempty\"`\n}}\n\n"
        f"func h{i}(ctx context.Context, r *mcp.CallToolRequest, in Args{i}) "
        f"(*mcp.CallToolResult, any, error) {{ return nil, nil, nil }}\n\n"
        for i in range(n)
    )
    adds = "".join(
        f"\tt{i} := &mcp.Tool{{Name: \"t{i}\", Description: \"Tool {i}\"}}\n"
        f"\tmcp.AddTool(server, t{i}, h{i})\n"
        f"\tserver.AddTool(&mcp.Tool{{Name: \"raw{i}\", InputSchema: &jsonschema.Schema{{"
        f"Type: \"object\", Properties: map[string]*jsonschema.Schema{{\"q\": {{Type: \"string\"}}}}}}}}, raw{i})\n"
        for i in range(n)
    )
    return (
        'package main\n\nimport (\n\t"context"\n\n\t"github.com/google/jsonschema-go/jsonschema"\n'
        '\t"github.com/modelcontextprotocol/go-sdk/mcp"\n)\n\n'
        f"{types}func main() {{\n\tserver := mcp.NewServer(nil, nil)\n{adds}}}\n"
    )


def _go_mcp_functions(n: int) -> str:
    """One function per tool, each with its own tool variable ``t``."""
    funcs = "".join(
        f"func reg{i}(s *mcp.Server) {{\n\tt := &mcp.Tool{{Name: \"t{i}\"}}\n"
        f"\tmcp.AddTool(s, t, h)\n}}\n\n"
        for i in range(n)
    )
    return f'package main\n\nimport "github.com/modelcontextprotocol/go-sdk/mcp"\n\n{funcs}'


def _java_mcp_shortcut(n: int) -> str:
    tools = "".join(
        f'            .tool(new McpSchema.Tool("t{i}", "Tool {i}", "{{}}"), (exchange, args) -> null)\n'
        f'            .toolCall(McpSchema.Tool.builder().name("c{i}").build(), this::call)\n'
        for i in range(n)
    )
    return (
        "package demo;\nimport io.modelcontextprotocol.server.McpServer;\n"
        "import io.modelcontextprotocol.spec.McpSchema;\n"
        "public class App {\n    void start(Object transport) {\n"
        f"        McpServer.sync(transport)\n{tools}            .build();\n    }}\n"
        "    Object call(Object exchange, Object request) { return null; }\n}\n"
    )


def _kotlin_mcp_add_tools(n: int) -> str:
    vals = "".join(
        f'val tools{i} = listOf(RegisteredTool(Tool(name = "t{i}", description = "Tool {i}")) {{ _ -> null }})\n'
        f'val mutable{i} = mutableListOf(RegisteredTool(Tool(name = "m{i}")) {{ _ -> null }})\n'
        f'fun built{i}() = listOf(RegisteredTool(Tool(name = "b{i}")) {{ _ -> null }})\n'
        for i in range(n)
    )
    calls = "".join(
        f"    server.addTools(tools{i})\n    server.addTools(mutable{i})\n"
        f"    server.addTools(built{i}())\n"
        for i in range(n)
    )
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.RegisteredTool\n"
        "import io.modelcontextprotocol.kotlin.sdk.Tool\n"
        "import io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        f"{vals}fun register(server: Server) {{\n{calls}}}\n"
    )


def _python_mcp_entries(n: int) -> str:
    entries = "".join(
        f"@mcp.resource('users://{{id}}/r{i}', description='R {i}')\ndef r{i}(id: str) -> str:\n    return id\n\n\n"
        f"@mcp.prompt(name='p{i}')\ndef p{i}(code: str, focus: str = 'x') -> str:\n    return code\n\n\n"
        for i in range(n)
    )
    lowlevel = "".join(
        f"        types.Resource(uri='memo://m{i}', name='m{i}'),\n" for i in range(n)
    )
    prompts = "".join(
        f"        types.Prompt(name='lp{i}', arguments=[types.PromptArgument(name='a', required=True)]),\n"
        for i in range(n)
    )
    calls = "".join(
        f"        await session.call_tool('t{i}', {{}})\n        await session.read_resource('memo://m{i}')\n"
        for i in range(n)
    )
    return (
        "from mcp.server.fastmcp import FastMCP\nfrom mcp.server.lowlevel import Server\n"
        "import mcp.types as types\nfrom mcp import ClientSession\n\nmcp = FastMCP('demo')\n"
        f"server = Server('low')\n\n\n{entries}"
        f"@server.list_resources()\nasync def lr():\n    return [\n{lowlevel}    ]\n\n\n"
        f"@server.list_prompts()\nasync def lp():\n    return [\n{prompts}    ]\n\n\n"
        "@server.read_resource()\nasync def rr(uri):\n    return ''\n\n\n"
        "async def client(read, write):\n    async with ClientSession(read, write) as session:\n"
        f"{calls}"
    )


def _typescript_mcp_entries(n: int) -> str:
    entries = "".join(
        f"server.registerResource('r{i}', new ResourceTemplate('users://{{id}}/r{i}', {{ list: undefined }}), "
        f"{{ description: 'R {i}' }}, async () => ({{ contents: [] }}));\n"
        f"server.prompt('p{i}', 'P {i}', {{ code: z.string().describe('c') }}, async () => ({{ messages: [] }}));\n"
        f"await client.callTool({{ name: 't{i}' }});\n"
        for i in range(n)
    )
    listed = ", ".join(f"{{ uri: 'memo://m{i}', name: 'm{i}' }}" for i in range(n))
    return (
        "import { McpServer, ResourceTemplate } from '@modelcontextprotocol/sdk/server/mcp.js';\n"
        "import { Server } from '@modelcontextprotocol/sdk/server/index.js';\n"
        "import { Client } from '@modelcontextprotocol/sdk/client/index.js';\n"
        "import { ListResourcesRequestSchema } from '@modelcontextprotocol/sdk/types.js';\n"
        "import { z } from 'zod';\n"
        "const server = new McpServer({ name: 'demo', version: '1' });\n"
        "const low = new Server({ name: 'low', version: '1' }, {});\n"
        "const client = new Client({ name: 'c', version: '1' });\n"
        # One function body: every registration and call looks up its receiver in one block.
        f"export async function main() {{\n{entries}}}\n"
        f"low.setRequestHandler(ListResourcesRequestSchema, async () => ({{ resources: [{listed}] }}));\n"
    )


def _go_mcp_entries(n: int) -> str:
    body = "".join(
        f"\tserver.AddResourceTemplate(&mcp.ResourceTemplate{{URITemplate: \"users://{{id}}/r{i}\"}}, h)\n"
        f"\tserver.AddPrompt(&mcp.Prompt{{Name: \"p{i}\", Arguments: []*mcp.PromptArgument{{{{Name: \"a\"}}}}}}, h)\n"
        f"\tsession.CallTool(ctx, &mcp.CallToolParams{{Name: \"t{i}\"}})\n"
        for i in range(n)
    )
    return (
        "package main\n\nimport \"github.com/modelcontextprotocol/go-sdk/mcp\"\n\n"
        "func main() {\n\tserver := mcp.NewServer(&mcp.Implementation{Name: \"x\"}, nil)\n"
        "\tclient := mcp.NewClient(&mcp.Implementation{Name: \"c\"}, nil)\n"
        f"\tsession, _ := client.Connect(ctx, t, nil)\n{body}}}\n\nfunc h() {{}}\n"
    )


def _go_mcpgo_entries(n: int) -> str:
    body = "".join(
        f"\ts.AddResource(mcp.NewResource(\"docs://d{i}\", \"d{i}\", mcp.WithResourceDescription(\"D\")), h)\n"
        f"\ts.AddPrompt(mcp.NewPrompt(\"p{i}\", mcp.WithArgument(\"a\", mcp.RequiredArgument())), h)\n"
        f"\treq{i} := mcp.CallToolRequest{{}}\n\treq{i}.Params.Name = \"t{i}\"\n\tc.CallTool(ctx, req{i})\n"
        for i in range(n)
    )
    return (
        "package main\n\nimport (\n\t\"github.com/mark3labs/mcp-go/client\"\n"
        "\t\"github.com/mark3labs/mcp-go/mcp\"\n\t\"github.com/mark3labs/mcp-go/server\"\n)\n\n"
        "func main() {\n\ts := server.NewMCPServer(\"x\", \"1\")\n"
        f"\tc, _ := client.NewStdioMCPClient(\"s\", nil)\n{body}}}\n\nfunc h() {{}}\n"
    )


def _java_mcp_entries(n: int) -> str:
    specs = "".join(
        f"        specs.add(new McpServerFeatures.SyncResourceTemplateSpecification(\n"
        f"            McpSchema.ResourceTemplate.builder(\"users://{{id}}/r{i}\", \"r\").build(), (a, b) -> null));\n"
        f"        specs.add(new McpServerFeatures.SyncPromptSpecification(new McpSchema.Prompt(\"p{i}\", \"d\",\n"
        f"            List.of(new McpSchema.PromptArgument(\"a\", \"d\", true))), (a, b) -> null));\n"
        f"        client.callTool(new McpSchema.CallToolRequest(\"t{i}\", Map.of()));\n"
        for i in range(n)
    )
    annotated = "".join(
        f"    @McpResource(uri = \"docs://d{i}\") String d{i}() {{ return \"\"; }}\n" for i in range(n)
    )
    return (
        "package demo;\nimport io.modelcontextprotocol.server.McpServerFeatures;\n"
        "import io.modelcontextprotocol.spec.McpSchema;\nimport io.modelcontextprotocol.client.McpSyncClient;\n"
        "import org.springaicommunity.mcp.annotation.McpResource;\n"
        f"public class App {{\n    McpSyncClient client;\n    void register(List<Object> specs) {{\n{specs}    }}\n"
        f"{annotated}}}\n"
    )


def _kotlin_mcp_entries(n: int) -> str:
    body = "".join(
        f"    server.addResource(uri = \"docs://d{i}\", name = \"d\", description = \"D\") {{ TODO() }}\n"
        f"    server.addPrompt(name = \"p{i}\", arguments = listOf(PromptArgument(name = \"a\", required = true))) {{ TODO() }}\n"
        f"    client.callTool(name = \"t{i}\", arguments = mapOf())\n"
        for i in range(n)
    )
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        "import io.modelcontextprotocol.kotlin.sdk.client.Client\n\n"
        f"suspend fun configure(server: Server, client: Client) {{\n{body}}}\n"
    )


def _csharp_mcp_entries(n: int) -> str:
    body = "".join(
        f"    [McpServerResource(UriTemplate = \"users://{{id}}/r{i}\"), Description(\"R\")]\n"
        f"    public static string R{i}(string id) => id;\n"
        f"    [McpServerPrompt(Name = \"p{i}\")]\n    public static string P{i}([Description(\"c\")] string code) => code;\n"
        f"    public static async Task C{i}(IMcpClient client{i}) => await client{i}.CallToolAsync(\"t{i}\");\n"
        for i in range(n)
    )
    return (
        "using System.ComponentModel;\nusing ModelContextProtocol.Server;\nusing ModelContextProtocol.Client;\n"
        f"public static class All\n{{\n{body}}}\n"
    )


def _kotlin_mcp_add_tools_functions(n: int) -> str:
    """One function per list, each with its own local ``tools`` of the same name."""
    funcs = "".join(
        f"fun reg{i}(server: Server) {{\n"
        f'    val tools = mutableListOf(RegisteredTool(Tool(name = "t{i}")) {{ _ -> null }})\n'
        f"    server.addTools(tools)\n}}\n"
        for i in range(n)
    )
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.RegisteredTool\n"
        "import io.modelcontextprotocol.kotlin.sdk.Tool\n"
        "import io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        f"{funcs}"
    )


def _kotlin_mcp_add_tools_one_list(n: int) -> str:
    """One ``mutableListOf`` property passed to many ``addTools`` calls."""
    calls = "    server.addTools(tools)\n" * n
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.RegisteredTool\n"
        "import io.modelcontextprotocol.kotlin.sdk.Tool\n"
        "import io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        'val tools = mutableListOf(RegisteredTool(Tool(name = "t")) { _ -> null })\n'
        f"fun register(server: Server) {{\n{calls}}}\n"
    )


def _typescript_mcp_named_handlers(n: int) -> str:
    """One function registering many tools, each with its own top-level handler by name: each
    handler's scope check stays at the reference's depth, never a re-read of the function."""
    regs = "".join(f'  server.tool("t{i}", "Tool {i}", {{}}, h{i});\n' for i in range(n))
    funcs = "".join(f"export function h{i}() {{ return {{ content: [] }}; }}\n" for i in range(n))
    return (
        "import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';\n"
        f"export function register(server: McpServer) {{\n{regs}}}\n{funcs}"
    )


def _java_mcp_method_refs(n: int) -> str:
    """Many specifications, each naming a different method of the class: the class body is
    indexed once, not scanned per reference."""
    specs = "".join(f"            new SyncToolSpecification(tool, this::h{i}),\n" for i in range(n))
    methods = "".join(f"    Object h{i}(Object e, Object a) {{ return null; }}\n" for i in range(n))
    return (
        "package demo;\n"
        "import io.modelcontextprotocol.server.McpServerFeatures.SyncToolSpecification;\n"
        "import io.modelcontextprotocol.spec.McpSchema;\n"
        'public class App {\n    McpSchema.Tool tool = new McpSchema.Tool("t", "d", "{}");\n'
        f"    java.util.List<Object> specs() {{\n        return java.util.List.of(\n{specs}"
        f"            null);\n    }}\n{methods}}}\n"
    )


def _kotlin_mcp_function_refs(n: int) -> str:
    """Many ``addTool`` calls, each naming a different top-level function (``::hN``)."""
    adds = "".join(
        f'    server.addTool(name = "t{i}", description = "Tool {i}", handler = ::h{i})\n'
        for i in range(n)
    )
    funs = "".join(f"fun h{i}(r: CallToolRequest): CallToolResult = TODO()\n" for i in range(n))
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        f"fun register(server: Server) {{\n{adds}}}\n{funs}"
    )


def _kotlin_mcp_method_refs(n: int) -> str:
    """Many ``addTool`` calls, each naming a different method of the class (``this::hN``)."""
    adds = "".join(
        f'        server.addTool(name = "t{i}", description = "Tool {i}", handler = this::h{i})\n'
        for i in range(n)
    )
    funs = "".join(f"    fun h{i}(r: CallToolRequest): CallToolResult = TODO()\n" for i in range(n))
    return (
        "package demo\nimport io.modelcontextprotocol.kotlin.sdk.server.Server\n"
        f"class Registry {{\n    fun register(server: Server) {{\n{adds}    }}\n{funs}}}\n"
    )


def _mdx(n: int) -> str:
    """ESM blocks, JSX, ``{…}`` expressions and code fences: the block probes, the shadow parse
    and the document reading grow with the file. One paragraph of prose that starts with
    ``import`` (up to 21 probes) is kept fixed, so neither size reaches the per-file block
    budget, which would flatten the cost and hide a regression."""
    parts = []
    for i in range(n):
        parts.append(
            f"import {{ C{i} }} from './c{i}'\n\n"
            f"## Section {i}\n\n"
            f"<C{i} value={{{{ n: {i} }}}} />\n\n"
            f"```js\nimport x{i} from 'x'\n```\n\n"
        )
    return "# Guide\n\nimport the data and run it.\n\n" + "".join(parts)


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
    "typescript-express-locals": ("app.js", _ts_express_locals),
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
    "mdx": ("docs/guide.mdx", _mdx),
    "python-mcp": ("server.py", _python_mcp),
    "typescript-mcp": ("server.ts", _typescript_mcp),
    "python-mcp-lowlevel": ("server.py", _python_mcp_lowlevel),
    "typescript-mcp-lowlevel": ("server.ts", _typescript_mcp_lowlevel),
    "typescript-mcp-named-handlers": ("server.ts", _typescript_mcp_named_handlers),
    "go-mcp": ("main.go", _go_mcp),
    "go-mcp-functions": ("tools.go", _go_mcp_functions),
    "java-mcp-shortcut": ("App.java", _java_mcp_shortcut),
    "java-mcp-method-refs": ("App.java", _java_mcp_method_refs),
    "kotlin-mcp-function-refs": ("Registry.kt", _kotlin_mcp_function_refs),
    "kotlin-mcp-method-refs": ("Registry.kt", _kotlin_mcp_method_refs),
    "kotlin-mcp-add-tools": ("Registry.kt", _kotlin_mcp_add_tools),
    "python-mcp-entries": ("server.py", _python_mcp_entries),
    "typescript-mcp-entries": ("server.ts", _typescript_mcp_entries),
    "go-mcp-entries": ("main.go", _go_mcp_entries),
    "go-mcpgo-entries": ("main.go", _go_mcpgo_entries),
    "java-mcp-entries": ("App.java", _java_mcp_entries),
    "kotlin-mcp-entries": ("Server.kt", _kotlin_mcp_entries),
    "csharp-mcp-entries": ("All.cs", _csharp_mcp_entries),
    "kotlin-mcp-add-tools-functions": ("Registry.kt", _kotlin_mcp_add_tools_functions),
    "kotlin-mcp-add-tools-one-list": ("Registry.kt", _kotlin_mcp_add_tools_one_list),
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
