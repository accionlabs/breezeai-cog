# mcp-go — entry-point roles

Parser: `src/breezeai_cog/parsers/go_mcpgo/tools.py` (additive detector) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_mcp_go.py`

A tool a Go MCP server declares with `github.com/mark3labs/mcp-go`. The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

| Construct | Role | Record | Why |
|---|---|---|---|
| `mcp.NewTool(name, mcp.WithDescription(…), mcp.WithString(…, mcp.Required(), mcp.Description(…), mcp.Default…(…)), …)` (`mark3labs/mcp-go`) | Binding | `mcp_tool`, `framework: mcp-go`, at `NewTool`; `params` from the `With<Type>` options, `null` (unknown) when an option is not one of them nor a known non-parameter option (description, title, annotations, output schema); `handler` = the second argument of the `AddTool`, or the `Handler` of the `server.ServerTool{Tool: …, Handler: …}` literal (also an element of `[]server.ServerTool{…}`), that registers it: directly, or through its variable — the registration of it after the assignment in the same function and before the variable is reassigned (several disagreeing → null); a package-level variable: the one registration from a function that does not declare its own variable of that name, only when nothing reassigns it (otherwise null); `mcp.NewTypedToolHandler(fn)` / `server.NewTypedToolHandler(fn)` → `fn` | The `mcp` package of mcp-go, under any import name |
| A computed tool name (`mcp.NewTool(name, …)` with a variable) | None | — | The tool exists, but its name is unknown |

Not covered yet (follow-up): `mcp_resource` / `mcp_prompt`, and MCP **client** calls. The official Go SDK is in the [go-mcp table](go-mcp.md).
