"""MCP server tools of the TypeScript SDK (``@modelcontextprotocol/sdk``):
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
"""

from __future__ import annotations

import json


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


# ── Resources and prompts ─────────────────────────────────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "typescript-mcp"
    return {s.endpoint: s for s in found}


TS_RESOURCES = """
import { McpServer, ResourceTemplate as Tpl } from "@modelcontextprotocol/sdk/server/mcp.js";

const server = new McpServer({ name: "res", version: "1.0.0" });

server.resource("config", "config://app", async (uri) => ({ contents: [] }));

server.resource("user", new Tpl("users://{id}", { list: undefined }),
  { description: "A user" }, readUser);

server.registerResource("logs", "file:///logs/app.log",
  { title: "Logs", description: "App log", mimeType: "text/plain" }, async () => ({ contents: [] }));

server.registerResource("search", new Tpl("search://{?q}", { list: undefined }), {}, readSearch);

server.registerResource("computed", `cfg://${env}`, {}, async () => ({ contents: [] }));

function readUser() { return { contents: [] }; }
function readSearch() { return { contents: [] }; }
"""


def test_typescript_resources_with_no_tool(analyze_files) -> None:
    resources = _of(analyze_files({"srv.ts": TS_RESOURCES}), "mcp_resource")
    assert set(resources) == {"config://app", "users://{id}", "file:///logs/app.log", "search://{?q}"}
    config = resources["config://app"]
    assert (config.method, config.params, config.description, config.handler) == (
        "RESOURCE", [], None, None)
    assert config.handlerLine == 6 and config.startLine == 6
    user = resources["users://{id}"]
    assert (user.description, user.handler, user.handlerLine) == ("A user", "readUser", 18)
    assert _params(user) == [("id", "string", True, None, None)]
    assert resources["file:///logs/app.log"].description == "App log"
    assert resources["search://{?q}"].params is None


TS_PROMPTS = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const server = new McpServer({ name: "prompts", version: "1.0.0" });

server.prompt("greet", async () => ({ messages: [] }));

server.prompt("review", "Review code",
  { code: z.string().describe("The code"), focus: z.string().optional() },
  async ({ code }) => ({ messages: [] }));

server.registerPrompt("explain",
  { title: "Explain", description: "Explain a topic", argsSchema: { topic: z.string() } },
  explainHandler);

server.registerPrompt("shaped", { argsSchema: shape }, async () => ({ messages: [] }));

server.prompt(promptName, async () => ({ messages: [] }));

function explainHandler() { return { messages: [] }; }
"""


def test_typescript_prompts_with_no_tool(analyze_files) -> None:
    prompts = _of(analyze_files({"srv.ts": TS_PROMPTS}), "mcp_prompt")
    assert set(prompts) == {"greet", "review", "explain", "shaped"}
    assert prompts["greet"].params == [] and prompts["greet"].method == "PROMPT"
    review = prompts["review"]
    assert review.description == "Review code"
    assert _params(review) == [
        ("code", None, True, None, "The code"),
        ("focus", None, False, None, None),
    ]
    explain = prompts["explain"]
    assert (explain.description, explain.handler, explain.handlerLine) == (
        "Explain a topic", "explainHandler", 21)
    assert _params(explain) == [("topic", None, True, None, None)]
    assert prompts["shaped"].params is None  # a variable shape: unknown


def test_typescript_resource_and_prompt_look_alikes(analyze_files) -> None:
    """Not an McpServer, or a ResourceTemplate from elsewhere: no record."""
    files = {
        "other.ts": """
import { Router } from "express";
const router = new Router();
router.resource("users", "users://x", async () => ({}));
router.prompt("p", async () => ({}));
""",
        "tpl.ts": """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ResourceTemplate } from "./my-templates";
const server = new McpServer({ name: "x", version: "1" });
server.resource("user", new ResourceTemplate("users://{id}", { list: undefined }), async () => ({}));
""",
    }
    statements = analyze_files(files)
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]


TS_LOWLEVEL_RESOURCES = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  ListResourcesRequestSchema, ListResourceTemplatesRequestSchema, ReadResourceRequestSchema,
  ListPromptsRequestSchema, GetPromptRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server({ name: "x", version: "1" }, { capabilities: {} });

server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    { uri: "console://logs", name: "Logs", description: "Console" },
    ...extra.map((n) => ({ uri: `shot://${n}`, name: n })),
    { uri: dynamicUri, name: "dyn" },
  ],
}));

server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => {
  return { resourceTemplates: [{ uriTemplate: "test://static/{id}", name: "Static" }] };
});

server.setRequestHandler(ReadResourceRequestSchema, readResource);

server.setRequestHandler(ListPromptsRequestSchema, async () => ({
  prompts: [
    { name: "simple", description: "No arguments" },
    { name: "complex", arguments: [
      { name: "temperature", description: "Temperature", required: true },
      { name: "style" },
    ] },
    { name: "dynamic", arguments: ARGS },
  ],
}));

server.setRequestHandler(GetPromptRequestSchema, async (request) => ({ messages: [] }));

function readResource() { return { contents: [] }; }
"""


def test_typescript_lowlevel_resources_and_prompts_with_no_tool(analyze_files) -> None:
    statements = analyze_files({"srv.ts": TS_LOWLEVEL_RESOURCES})
    resources = _of(statements, "mcp_resource")
    assert set(resources) == {"console://logs", "test://static/{id}"}
    logs = resources["console://logs"]
    assert (logs.description, logs.handler, logs.handlerLine, logs.params) == (
        "Console", "readResource", 37, [])
    assert _params(resources["test://static/{id}"]) == [("id", "string", True, None, None)]
    assert resources["test://static/{id}"].handler == "readResource"
    prompts = _of(statements, "mcp_prompt")
    assert set(prompts) == {"simple", "complex", "dynamic"}
    assert prompts["simple"].params == [] and prompts["simple"].handlerLine == 35
    assert _params(prompts["complex"]) == [
        ("temperature", None, True, None, "Temperature"),
        ("style", None, False, None, None),
    ]
    assert prompts["dynamic"].params is None


def test_typescript_lowlevel_resource_schema_from_elsewhere_is_not_read(analyze_files) -> None:
    src = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListResourcesRequestSchema } from "./schemas";
const server = new Server({ name: "x", version: "1" }, {});
server.setRequestHandler(ListResourcesRequestSchema, async () => ({ resources: [{ uri: "a://b", name: "b" }] }));
"""
    assert not _of(analyze_files({"srv.ts": src}), "mcp_resource")
