# MCP TypeScript SDK — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_mcp/tools.py` (additive detector) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_typescript_mcp.py`

A tool of a server built with `@modelcontextprotocol/sdk`, in TypeScript or JavaScript. The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

A tool is recorded only when the object it is registered on is **proven an MCP server** and its name is a literal; `.tool(…)` on any other object is left alone.

| Construct | Role | Record | Why |
|---|---|---|---|
| `<server>.tool(name, [description], [zodShape], [annotations], callback)` (`@modelcontextprotocol/sdk`) | Binding | `mcp_tool`, `framework: typescript-mcp`, at the call; an object is the params shape only when its values are zod chains (`.describe`, `.optional`, `.default`, `z.enum([…])`; `z.optional(X)` / `z.nullish(X)` optional with `X`'s type, `z.nullable(X)` still required); `z.object({…}).partial()` / `.deepPartial()` makes every param optional, and a method that changes the keys (`.extend`, `.merge`, `.pick`, `.omit`, `.partial({…})`) leaves `params` null | `<server>` is assigned `new McpServer(…)` (a variable, `this.x`, or a class field) or typed `McpServer`; a wrapped callback counts: the handler is the function literal inside (`withErrors(async (a) => …)`, its line), or a function of the same file passed alone (`withErrors(namedHandler)`); any other wrapper argument (`createHandler(apiClient)`) is not a handler — the record keeps only the wrapper's line |
| `<server>.registerTool(name, {description, inputSchema}, callback)` | Binding | as above; `inputSchema` = a zod raw shape, `z.object({…})`, or a JSON-schema literal (`properties` / `required`) | |
| A computed tool name (`server.tool(NAME, …)`) | None | — | The tool exists, but its name is unknown |
| `.tool(…)` / `.registerTool(…)` on any other object, or on a name that shadows the server | None | — | Not an MCP server tool |

Not covered yet (follow-up): the low-level API (`setRequestHandler(ListToolsRequestSchema, …)`), `mcp_resource` / `mcp_prompt`, and MCP **client** calls.
