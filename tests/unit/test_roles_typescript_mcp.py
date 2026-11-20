"""MCP server tools of the TypeScript SDK (``@modelcontextprotocol/sdk``):
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
"""

from __future__ import annotations

import json

import pytest

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


TS_SERVER = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const server = new McpServer({ name: "demo", version: "1.0.0" });

server.tool("add", "Add two numbers",
  { a: z.number().describe("First"), b: z.number().optional().default(2) },
  async ({ a, b }) => ({ content: [] }));

server.tool("ping", { readOnlyHint: true }, async () => ({ content: [] }));

server.tool("lookup", { id: z.string() }, { readOnlyHint: true }, lookupHandler);

server.registerTool("echo",
  { title: "Echo", description: "Echo a message", inputSchema: { msg: z.string(), mode: z.enum(["a", "b"]) } },
  async ({ msg }) => ({ content: [] }));

async function lookupHandler({ id }) { return { content: [] }; }

const other = makeAgent();
other.tool("notAnMcpTool", async () => 1);
"""


def test_typescript_sdk_tool_overloads(analyze_files) -> None:
    tools = _tools(analyze_files({"src/server.ts": TS_SERVER}))
    assert set(tools) == {"add", "ping", "lookup", "echo"}

    add = tools["add"]
    assert add.framework == "typescript-mcp" and add.description == "Add two numbers"
    assert add.handler is None and add.handlerLine == 9  # inline callback: its line
    assert _params(add) == [("a", "number", True, None, "First"), ("b", "number", False, "2", None)]

    ping = tools["ping"]  # (name, annotations, cb): the annotations are not params
    assert ping.params == [] and ping.description is None

    lookup = tools["lookup"]  # (name, shape, annotations, cb) with a named handler
    assert _params(lookup) == [("id", "string", True, None, None)]
    assert (lookup.handler, lookup.handlerLine) == ("lookupHandler", 19)

    echo = tools["echo"]
    assert echo.description == "Echo a message"
    assert _params(echo) == [
        ("msg", "string", True, None, None),
        ("mode", "enum(a,b)", True, None, None),
    ]


def test_typescript_class_field_server_and_wrapped_callback(analyze_files) -> None:
    src = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

class Srv {
  private server = new McpServer({ name: "x", version: "1" });
  init() {
    this.server.tool("search", "Search", { q: z.string() }, withErrors(async ({ q }) => ({ content: [] })));
    this.server.tool("named", withErrors(namedHandler));
  }
}
async function namedHandler() { return { content: [] }; }
"""
    tools = _tools(analyze_files({"src/srv.ts": src}))
    assert set(tools) == {"search", "named"}
    assert tools["search"].description == "Search" and tools["search"].handlerLine == 8
    assert tools["named"].handler == "namedHandler"


TS_HEADER = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const server = new McpServer({ name: "demo", version: "1.0.0" });
"""


def test_typescript_unreadable_schema_is_unknown_not_empty(analyze_files) -> None:
    src = (
        TS_HEADER
        + """
server.tool("get", "Get one", { id: idSchema }, async ({ id }) => ({ content: [] }));
server.tool("spread", { ...common, q: z.string() }, async () => ({ content: [] }));
server.tool("coerce", { n: z.coerce.number() }, async () => ({ content: [] }));
server.tool("shared", "Shared", sharedShape, async () => ({ content: [] }));
server.registerTool("reg", { description: "R", inputSchema: MySchema.shape }, async () => ({ content: [] }));
server.registerTool("json", { inputSchema: { type: "object", properties: { a: { type: "string" } }, required: fields } }, async () => ({ content: [] }));
server.tool("empty", "Nothing", {}, async () => ({ content: [] }));
server.tool("none", "No schema", async () => ({ content: [] }));
server.registerTool("plain", { description: "P" }, async () => ({ content: [] }));
"""
    )
    tools = _tools(analyze_files({"src/server.ts": src}))
    for name in ("get", "spread", "coerce", "shared", "reg", "json"):
        assert tools[name].params is None, name
    assert tools["get"].description == "Get one"
    for name in ("empty", "none", "plain"):
        assert tools[name].params == [], name


def test_typescript_zod_wrappers_and_partial_objects(analyze_files) -> None:
    src = (
        TS_HEADER
        + """
server.tool("wrap", {
  a: z.optional(z.string().describe("A")),
  b: z.nullable(z.number()),
  c: z.nullish(z.boolean()),
  d: z.optional(someSchema),
}, async () => ({ content: [] }));
server.registerTool("part", { inputSchema: z.object({ x: z.string(), y: z.number() }).partial() }, async () => ({ content: [] }));
server.registerTool("deep", { inputSchema: z.object({ x: z.string() }).strict().deepPartial() }, async () => ({ content: [] }));
server.registerTool("some", { inputSchema: z.object({ x: z.string(), y: z.number() }).partial({ x: true }) }, async () => ({ content: [] }));
server.registerTool("ext", { inputSchema: z.object({ x: z.string() }).extend({ y: z.number() }) }, async () => ({ content: [] }));
server.registerTool("whole", { inputSchema: z.object({ x: z.string() }).describe("in") }, async () => ({ content: [] }));
"""
    )
    tools = _tools(analyze_files({"src/server.ts": src}))
    assert _params(tools["wrap"]) == [
        ("a", "string", False, None, "A"),
        ("b", "number", True, None, None),  # nullable: the key is still required
        ("c", "boolean", False, None, None),
        ("d", None, False, None, None),  # the wrapped schema is a variable: type unknown
    ]
    assert _params(tools["part"]) == [
        ("x", "string", False, None, None),
        ("y", "number", False, None, None),
    ]
    assert _params(tools["deep"]) == [("x", "string", False, None, None)]
    assert tools["some"].params is None and tools["ext"].params is None
    assert _params(tools["whole"]) == [("x", "string", True, None, None)]


def test_typescript_handler_factory_argument_is_not_the_handler(analyze_files) -> None:
    files = {
        "src/client.ts": "export const apiClient = { get() {} };\n",
        "src/server.ts": TS_HEADER
        + """import { apiClient } from "./client";
server.tool("search", "Search", { q: z.string() }, createHandler(apiClient));
server.tool("wrapped", "W", withRetry(async () => ({ content: [] })));
""",
    }
    tools = _tools(analyze_files(files))
    search = tools["search"]
    assert search.handler is None and search.handlerPath is None
    assert search.handlerLine == 7  # the factory call builds the handler there
    assert tools["wrapped"].handler is None and tools["wrapped"].handlerLine == 8


def test_typescript_shadowed_server_name_is_not_the_server(analyze_files) -> None:
    src = (
        TS_HEADER
        + """
server.tool("top", "Top level", async () => ({ content: [] }));
function f(server: Registry) { server.tool("fakeParam", async () => ({ content: [] })); }
const g = (server) => server.tool("fakeArrow", async () => ({ content: [] }));
function h({ server }) { server.tool("fakeDestructured", async () => ({ content: [] })); }
function k() {
  const server = new Registry();
  server.tool("fakeLocal", async () => ({ content: [] }));
}
function register(server: McpServer) {
  server.tool("typed", "Typed param", async () => ({ content: [] }));
}
function inner() {
  const server = new McpServer({ name: "inner", version: "1" });
  server.tool("innerLocal", async () => ({ content: [] }));
}
"""
    )
    assert set(_tools(analyze_files({"src/server.ts": src}))) == {"top", "typed", "innerLocal"}


def test_typescript_param_descriptions_stay_under_the_params_limit(analyze_files) -> None:
    """Every SDK keeps a tool's params JSON under the backend's limit, as Python does:
    descriptions are kept in param order while they fit, names and types always."""
    shape = ", ".join(f'p{i}: z.string().describe("{"x" * 1000}")' for i in range(20))
    src = TS_HEADER + f'server.tool("big", {{ {shape} }}, async () => ({{ content: [] }}));\n'
    params = _tools(analyze_files({"src/server.ts": src}))["big"].params or []
    assert [p.name for p in params] == [f"p{i}" for i in range(20)]
    assert all(p.type == "string" for p in params)
    documented = [bool(p.description) for p in params]
    assert 0 < sum(documented) < len(params)
    assert documented == sorted(documented, reverse=True)
    size = len(json.dumps([p.model_dump() for p in params], separators=(",", ":")).encode())
    assert size <= 15 * 1024


# ── Low-level server (`setRequestHandler(ListToolsRequestSchema, …)`) ────────

TS_LOWLEVEL = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema, Tool } from "@modelcontextprotocol/sdk/types.js";

const SEARCH_TOOL: Tool = {
  name: "search",
  description: "Search the web. " +
    "At most 20 results.",
  inputSchema: {
    type: "object",
    properties: { query: { type: "string", description: "Query" }, count: { type: "number" } },
    required: ["query"],
  },
};
let MUTABLE = { name: "mutable", inputSchema: { type: "object", properties: {} } };

const server = new Server({ name: "demo", version: "1" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    SEARCH_TOOL,
    MUTABLE,
    { name: "echo", description: `Echo`, inputSchema: { type: "object", properties: {} } },
    { name: ToolName.ADD, inputSchema: { type: "object", properties: {} } },
    { name: "zod", inputSchema: zodToJsonSchema(Args) as ToolInput },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  return { content: [] };
});
"""


def test_typescript_lowlevel_server_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"src/index.ts": TS_LOWLEVEL}))
    assert set(tools) == {"search", "echo", "zod"}  # a `let` and a computed name are not read
    search = tools["search"]
    assert (search.framework, search.description) == (
        "typescript-mcp",
        "Search the web. At most 20 results.",
    )
    assert (search.handler, search.handlerLine) == (None, 29)  # the dispatcher's line
    assert search.startLine == 5  # the const's object
    assert _params(search) == [
        ("query", "string", True, None, "Query"),
        ("count", "number", False, None, None),
    ]
    assert tools["echo"].params == [] and tools["echo"].description == "Echo"
    assert tools["zod"].params is None  # a schema that is not a literal: unknown


def test_typescript_lowlevel_return_statement_named_dispatcher_and_mcp_server(
    analyze_files,
) -> None:
    src = """
import * as sdk from "@modelcontextprotocol/sdk/server/index";
import * as types from "@modelcontextprotocol/sdk/types";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

class App {
  private server = new sdk.Server({ name: "x", version: "1" }, {});
  constructor() {
    this.server.setRequestHandler(types.ListToolsRequestSchema, async () => {
      const unused = () => ({ tools: [{ name: "nested" }] });
      return { tools: [{ name: "from_class", inputSchema: { type: "object", properties: {} } }] };
    });
    this.server.setRequestHandler(types.CallToolRequestSchema, dispatch);
  }
}

const mcp = new McpServer({ name: "y", version: "1" });
mcp.server.setRequestHandler(types.ListToolsRequestSchema, async () => ({ tools: [{ name: "low" }] }));

async function dispatch(request) { return { content: [] }; }
"""
    tools = _tools(analyze_files({"src/app.ts": src}))
    assert set(tools) == {"from_class", "low"}
    assert (tools["from_class"].handler, tools["from_class"].handlerLine) == ("dispatch", 20)
    assert tools["low"].handler is None and tools["low"].params is None  # no inputSchema


def test_typescript_lowlevel_dispatcher_of_another_server_is_not_the_handler(analyze_files) -> None:
    src = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

function a() {
  const server = new Server({ name: "a", version: "1" }, {});
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "only_a" }] }));
}
function b() {
  const server = new Server({ name: "b", version: "1" }, {});
  server.setRequestHandler(CallToolRequestSchema, async () => ({ content: [] }));
}
"""
    tools = _tools(analyze_files({"src/two.ts": src}))
    assert set(tools) == {"only_a"} and tools["only_a"].handlerLine is None


def test_typescript_lowlevel_look_alikes_are_not_tools(analyze_files) -> None:
    files = {
        # socket.io's Server, with the SDK's schema
        "src/socket.ts": """
import { Server } from "socket.io";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
const server = new Server();
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "socket" }] }));
""",
        # the SDK's Server, with a schema of the same name from elsewhere
        "src/schema.ts": """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "./my-schemas";
const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "local_schema" }] }));
""",
        # pg's Client and any other object with a setRequestHandler
        "src/pg.ts": """
import { Client } from "pg";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
const client = new Client();
client.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "pg" }] }));
function f(server: Registry) {
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "param" }] }));
}
""",
        # the Vercel AI SDK's tool()
        "src/ai.ts": """
import { tool } from "ai";
import { z } from "zod";
export const weather = tool({ description: "Weather", parameters: z.object({ city: z.string() }) });
""",
    }
    assert _tools(analyze_files(files)) == {}


def test_typescript_lowlevel_reads_only_module_level_consts(analyze_files) -> None:
    """``tools: [SEARCH]`` names the imported ``SEARCH``, not a ``const`` of the same name
    inside some function; a module-level ``const`` is read only when nothing else in the file
    declares its name."""
    src = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { SEARCH } from "./tools";

export const KEPT = { name: "kept" };
const SHADOWED = { name: "shadowed" };

function helper() {
  const SEARCH = { name: "fake" };
  return SEARCH;
}

const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [SEARCH, KEPT, SHADOWED] }));

function other(SHADOWED: unknown) { return SHADOWED; }
"""
    assert set(_tools(analyze_files({"src/index.ts": src}))) == {"kept"}


def test_typescript_handler_line_is_never_a_same_named_class_method(analyze_files) -> None:
    """A handler passed by name is the module function, not a class method of the same name
    declared after it; a name only a method has gives no line."""
    src = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

async function lookup(args) { return { content: [] }; }
async function dispatch(request) { return { content: [] }; }

class Tools {
  lookup() {}
  dispatch() {}
  lone() {}
}

const mcp = new McpServer({ name: "y", version: "1" });
mcp.tool("lookup", {}, lookup);
mcp.tool("lone", {}, lone);

const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "low" }] }));
server.setRequestHandler(CallToolRequestSchema, dispatch);
"""
    tools = _tools(analyze_files({"src/index.ts": src}))
    assert (tools["lookup"].handler, tools["lookup"].handlerLine) == ("lookup", 6)
    assert (tools["lone"].handler, tools["lone"].handlerLine) == ("lone", None)
    assert (tools["low"].handler, tools["low"].handlerLine) == ("dispatch", 7)


def test_typescript_lowlevel_server_given_two_list_handlers_gives_no_tools(analyze_files) -> None:
    """Each ``setRequestHandler(ListToolsRequestSchema, …)`` replaces the one before: with two on
    one server which list is served is not read; another server's list is kept."""
    src = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "first" }] }));
server.setRequestHandler(ListToolsRequestSchema, listAll);

const other = new Server({ name: "y", version: "1" }, {});
other.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "only" }] }));

async function listAll() { return { tools: [] }; }
"""
    assert set(_tools(analyze_files({"src/index.ts": src}))) == {"only"}


def test_typescript_handler_line_is_never_a_nested_function_or_an_import(analyze_files) -> None:
    """A handler passed by name gets the line of a function declared at the top of the file
    (or exported) only: a same-named function nested in another one is not what the name
    reaches, and a name the file imports is declared elsewhere."""
    src = (
        'import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"; '
        'import { lookupHandler } from "./h"; import { z } from "zod"; '
        'const server = new McpServer({name:"demo",version:"1.0.0"}); '
        'server.tool("lookup", { id: z.string() }, lookupHandler); '
        'function boot(app) { app.post("/x", function lookupHandler(req, res) { return 1; }); }'
    )
    tool = _tools(analyze_files({"src/server.ts": src}))["lookup"]
    assert (tool.handler, tool.handlerLine) == ("lookupHandler", None)

    nested_only = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const mcp = new McpServer({ name: "y", version: "1" });
mcp.tool("inner", {}, inner);
mcp.tool("top", {}, top);
mcp.tool("exported", {}, exported);
mcp.tool("arrow", {}, arrow);

const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "low" }] }));
server.setRequestHandler(CallToolRequestSchema, dispatch);

function boot() {
  function inner() { return 1; }
  function dispatch(request) { return 2; }
}
async function top() { return { content: [] }; }
export async function exported() { return { content: [] }; }
const arrow = async () => ({ content: [] });
"""
    tools = _tools(analyze_files({"src/index.ts": nested_only}))
    assert (tools["inner"].handler, tools["inner"].handlerLine) == ("inner", None)
    assert (tools["low"].handler, tools["low"].handlerLine) == ("dispatch", None)
    assert tools["top"].handlerLine == 20
    assert tools["exported"].handlerLine == 21
    assert tools["arrow"].handlerLine == 22


def test_typescript_handler_name_bound_by_an_enclosing_scope_is_not_the_top_level_function(
    analyze_files,
) -> None:
    """A handler name that a parameter, a local, a ``catch`` or loop variable or a nested
    declaration binds at the call is that local, not the top-level function of the name: no
    line (``tool`` / ``registerTool``, a wrapper's argument, and the low-level dispatcher). A
    ``var`` binds it in the whole function, even from a sibling block. The top-level function
    still gives its line where nothing binds the name, or a ``let`` binds it in another block."""
    src = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

async function lookup(args) { return { content: [] }; }
async function dispatch(request) { return { content: [] }; }

function reg(mcp: McpServer, lookup: any) {
  mcp.tool("param", {}, lookup);
}
function reg2(mcp: McpServer) {
  const lookup = makeHandler();
  mcp.tool("localconst", {}, lookup);
}
function reg3(s: Server, dispatch: any) {
  s.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "low" }] }));
  s.setRequestHandler(CallToolRequestSchema, dispatch);
}
const reg4 = (mcp: McpServer, { lookup }) => mcp.tool("destructured", {}, lookup);
function reg6(mcp: McpServer, all) {
  for (const lookup of all) { mcp.registerTool("forof", { description: "d" }, lookup); }
  try { run(); } catch (lookup) { mcp.tool("caught", {}, lookup); }
  mcp.tool("wrapped", {}, withErrors(lookup));
  class lookup {}
}
const mcp = new McpServer({ name: "y", version: "1" });
mcp.tool("top", {}, lookup);
mcp.tool("topwrapped", {}, withErrors(lookup));
function reg7(m: McpServer, other) {
  m.tool("unbound", {}, lookup);
}
function reg5(m: McpServer, ok) {
  if (ok) { var lookup = makeHandler(); }
  m.tool("siblingvar", {}, lookup);
}
function reg8(m: McpServer, ok) {
  if (ok) { let lookup = makeHandler(); }
  m.tool("siblinglet", {}, lookup);
}
"""
    tools = _tools(analyze_files({"src/index.ts": src}))
    for name in ("param", "localconst", "destructured", "forof", "caught", "siblingvar"):
        assert (tools[name].handler, tools[name].handlerLine) == ("lookup", None), name
    assert (tools["low"].handler, tools["low"].handlerLine) == ("dispatch", None)
    # A wrapper of a local is not looked into: no handler, the wrapper's line.
    assert (tools["wrapped"].handler, tools["wrapped"].handlerLine) == (None, 24)
    assert (tools["top"].handler, tools["top"].handlerLine) == ("lookup", 6)
    assert (tools["topwrapped"].handler, tools["topwrapped"].handlerLine) == ("lookup", 6)
    assert (tools["unbound"].handler, tools["unbound"].handlerLine) == ("lookup", 6)
    # A `let` in a sibling block binds only that block: the top-level function's line.
    assert (tools["siblinglet"].handler, tools["siblinglet"].handlerLine) == ("lookup", 6)


def test_typescript_server_is_proven_by_its_binding_not_its_name(analyze_files) -> None:
    """A server proven in one scope does not make a same-named receiver of another scope a
    server: a module-level name assigned something else, a ``var`` declared in a nested block
    (it binds the whole function), or ``this.x`` of a class other than the one whose field is
    the server. Each gives no record; the proven bindings next to them still do."""
    files = {
        "src/proxy.ts": """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
function build() { const server = new Server({ name: "s", version: "1" }, { capabilities: {} }); return server; }
const server = makeProxy();
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "unproven", description: "d", inputSchema: { type: "object" } }] }));
""",
        "src/var.ts": """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
function setup(server: Server) {
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "typed" }] }));
}
function other(o) {
  if (o) { var server = o.server; }
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "hoisted" }] }));
}
function mcpSetup(mcp: McpServer) { mcp.tool("mcpTyped", async () => ({ content: [] })); }
function mcpOther(o) {
  if (o) { var mcp = o.mcp; }
  mcp.tool("mcpHoisted", async () => ({ content: [] }));
}
""",
        "src/classes.ts": """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
class A {
  server = new Server({ name: "a", version: "1" }, {});
  mcp = new McpServer({ name: "a", version: "1" });
  start() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "fieldA" }] }));
    this.mcp.tool("mcpFieldA", async () => ({ content: [] }));
  }
}
class B {
  constructor(server, mcp) { this.server = server; this.mcp = mcp; }
  start() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "otherClass" }] }));
    this.mcp.tool("mcpOtherClass", async () => ({ content: [] }));
    this.mcp.server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "otherLow" }] }));
  }
}
class C {
  server = new Server({ name: "c", version: "1" }, {});
  start() {
    run(function () {
      this.server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "ownThis" }] }));
    });
  }
}
""",
    }
    assert set(_tools(analyze_files(files))) == {"typed", "mcpTyped", "fieldA", "mcpFieldA"}


def test_typescript_server_proven_by_assignment_typed_field_or_constructor(analyze_files) -> None:
    """A server assigned to a declared name, a field typed ``McpServer`` and a field assigned in
    the constructor are each proven; a destructured parameter or default naming the handler
    binds it (no line); ``this.x`` outside any class is no server."""
    src = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

async function lookup() { return { content: [] }; }

let server;
function init() { server = new McpServer({ name: "a", version: "1" }); }
server.tool("assigned", {}, lookup);

class Typed {
  private mcp: McpServer;
  start() { this.mcp.tool("typedField", async () => ({ content: [] })); }
}
class Built {
  constructor() { this.srv = new McpServer({ name: "b", version: "1" }); }
  start() { this.srv.tool("ctorField", async () => ({ content: [] })); }
}
function reg(m: McpServer, { a: lookup }, [other = lookup2]) {
  m.tool("pairPattern", {}, lookup);
}
function reg2(m: McpServer, [lookup = null]) {
  m.tool("arrayDefault", {}, lookup);
}
this.server.tool("noClass", async () => ({ content: [] }));
"""
    tools = _tools(analyze_files({"src/index.ts": src}))
    assert set(tools) == {"assigned", "typedField", "ctorField", "pairPattern", "arrayDefault"}
    assert (tools["assigned"].handler, tools["assigned"].handlerLine) == ("lookup", 4)
    for name in ("pairPattern", "arrayDefault"):
        assert (tools[name].handler, tools[name].handlerLine) == ("lookup", None), name


def test_typescript_parameter_of_a_signature_without_body_proves_no_server(analyze_files) -> None:
    """A parameter typed ``Server`` / ``McpServer`` in a signature with no body (an interface
    method, an overload, an abstract method, a function or constructor type, a call or construct
    signature) declares nothing a call can refer to: a module-level name of the same spelling
    (here an import) is not proven by it."""
    lowlevel = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { server } from "./bus";

export type Wire = (server: Server) => void;
export type Make = new (server: Server) => object;
interface Plugin { install(server: Server): void; (server: Server): void; new (server: Server): Plugin }
declare function overload(server: Server): void;
abstract class Base { abstract wire(server: Server): void; }

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ name: "busTool", inputSchema: { type: "object" } }],
}));
"""
    mcp = """
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { server } from "./agent-registry";

export interface Plugin {
  install(server: McpServer): void;
}
export function register(server: McpServer): void;
export function register(server: unknown) {}
abstract class Base { abstract wire(server: McpServer): void; }

server.tool("notAnMcpTool", async () => "x");
"""
    files = {"src/low.ts": lowlevel, "src/mcp.ts": mcp}
    assert _tools(analyze_files(files)) == {}


def test_typescript_this_in_an_object_literal_method_is_not_the_class(analyze_files) -> None:
    """An object literal's method (a getter or setter too) has its own ``this``: ``this.server``
    there is the literal's property, not the field of the class around it. The class's own
    method and an arrow inside it still see the class's field."""
    src = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

export class Host {
  server = new Server({ name: "x", version: "1" }, { capabilities: { tools: {} } });
  mcp = new McpServer({ name: "y", version: "1" });
  wire(other: any) {
    const plugin = {
      server: other,
      mcp: other,
      install() {
        this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
          tools: [{ name: "pluginTool", inputSchema: { type: "object" } }],
        }));
        this.mcp.tool("pluginMcpTool", async () => ({ content: [] }));
      },
      get wired() {
        this.mcp.tool("getterTool", async () => ({ content: [] }));
        return true;
      },
      set wired(v) {
        this.mcp.tool("setterTool", async () => ({ content: [] }));
      },
    };
    const arrow = () => this.mcp.tool("classTool", async () => ({ content: [] }));
    plugin.install();
  }
}
"""
    assert set(_tools(analyze_files({"src/host.ts": src}))) == {"classTool"}


def test_typescript_lowlevel_string_with_an_escape_is_not_read(analyze_files) -> None:
    """An escape sequence is not decoded, so a name or description holding one is not read
    (``"a\\x62c"`` is ``abc``, not ``a\\x62c``); a plain string next to it still is."""
    src = r"""
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    { name: "a\x62c" },
    { name: "q\"t" },
    { name: `x\ty` },
    { name: "pre" + "\x41" },
    { name: "plain", description: "line\nbreak" },
    { name: `tmpl`, description: 'it\'s' },
  ],
}));
"""
    tools = _tools(analyze_files({"src/esc.ts": src}))
    assert set(tools) == {"plain", "tmpl"}
    assert tools["plain"].description is None and tools["tmpl"].description is None


_WRITTEN_SRC = """
import {{ McpServer }} from "@modelcontextprotocol/sdk/server/mcp.js";
import {{ Server }} from "@modelcontextprotocol/sdk/server/index.js";
import {{ ListToolsRequestSchema }} from "@modelcontextprotocol/sdk/types.js";

let server = new McpServer({{ name: "a", version: "1" }});
let low = new Server({{ name: "b", version: "1" }}, {{}});
class Host {{
  mcp = new McpServer({{ name: "c", version: "1" }});
  start() {{
    {write}
    this.mcp.tool("field", async () => ({{ content: [] }}));
  }}
}}
function later() {{
  {write}
}}
server.tool("high", async () => ({{ content: [] }}));
low.setRequestHandler(ListToolsRequestSchema, async () => ({{ tools: [{{ name: "low" }}] }}));
"""


@pytest.mark.parametrize(
    ("write", "unproven"),
    [
        ("server = makeProxy();", {"high"}),
        ("server ||= makeProxy();", {"high"}),
        ("server += 1;", {"high"}),
        ("server++;", {"high"}),
        ("[server] = proxies;", {"high"}),
        ("[first, ...server] = proxies;", {"high"}),
        ("({ server } = registry);", {"high"}),
        ("({ s: server = fallback } = registry);", {"high"}),
        ("for (server of proxies) {}", {"high"}),
        ("for (server in registry) {}", {"high"}),
        ("for ([server] of pairs) {}", {"high"}),
        ("server! = makeProxy();", {"high"}),
        ("(server as any) = makeProxy();", {"high"}),
        ("low = wrap(low);", {"low"}),
        ("this.mcp = makeProxy();", {"field"}),
        ("[this.mcp] = proxies;", {"field"}),
    ],
)
def test_typescript_server_written_anything_else_is_not_proven(
    analyze_files, write, unproven
) -> None:
    """A binding is a server only when every write to it is ``new McpServer(…)`` / ``new
    Server(…)``: an assignment of anything else (a compound one, ``++``, a destructuring
    target, a ``for … of`` / ``in`` without a declaration), wherever the binding is seen, makes
    it unproven — no record. The other servers of the file keep their tools."""
    all_tools = {"high", "low", "field"}
    assert set(_tools(analyze_files({"src/a.ts": _WRITTEN_SRC.format(write="")}))) == all_tools
    src = _WRITTEN_SRC.format(write=write)
    assert set(_tools(analyze_files({"src/a.ts": src}))) == all_tools - unproven


@pytest.mark.parametrize(
    "write",
    [
        "let server = makeProxy();",
        "const { server } = registry;",
        "for (const server of proxies) {}",
        "for (let server in registry) {}",
        "try {} catch (server) { server = 1; }",
        "function inner(server) { server = makeProxy(); }",
        "const f = (server) => { server++; };",
        "server = new McpServer({ name: 'again', version: '1' });",
        "let other = makeProxy(); other = server;",
        "let server;",
    ],
)
def test_typescript_server_kept_when_another_binding_is_written(analyze_files, write) -> None:
    """A write to another binding of the same name (a local, a destructured local, a loop's own
    variable, a ``catch`` or a parameter of a nested function), a second ``new McpServer(…)``, a
    write to another name, and a declaration without a value leave the proven server as it
    is."""
    src = _WRITTEN_SRC.format(write=write)
    assert set(_tools(analyze_files({"src/a.ts": src}))) == {"high", "low", "field"}


def test_typescript_var_loop_variable_writes_its_function_binding(analyze_files) -> None:
    """``for (var s of …)`` writes the function's ``s`` (a ``var`` belongs to the function): a
    server declared there is no longer proven. ``for (let s of …)`` is the loop's own."""
    src = """
import {{ McpServer }} from "@modelcontextprotocol/sdk/server/mcp.js";
function build(xs) {{
  var server = new McpServer({{ name: "a", version: "1" }});
  for ({kind} server of xs) {{}}
  server.tool("built", async () => ({{ content: [] }}));
}}
"""
    assert _tools(analyze_files({"src/a.ts": src.format(kind="var")})) == {}
    assert set(_tools(analyze_files({"src/a.ts": src.format(kind="let")}))) == {"built"}
