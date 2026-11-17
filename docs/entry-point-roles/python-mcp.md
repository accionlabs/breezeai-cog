# MCP Python SDK — entry-point roles

Parser: `src/breezeai_cog/parsers/python_mcp/tools.py` (additive detector); the `@<server>.tool` reader and the `mcp.servers` fact are in `parsers/python_mcp_common.py`, shared with [`fastmcp`](fastmcp.md) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_python_mcp.py`

A tool of a server built with the official Python SDK (`mcp.server.fastmcp`). The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

Both packages declare a tool the same way, so one reader serves both: every rule of the [FastMCP table](fastmcp.md) for `endpoint`, `description`, `params` and the skipped parameters applies here unchanged. Only the package that built the server differs.

| Construct | Role | Record | Why |
|---|---|---|---|
| `@<server>.tool` / `@<server>.tool(name=…, description=…)` on a function, the server built with `FastMCP` imported from `mcp.server.fastmcp` | Binding | `mcp_tool`, `framework: python-mcp`, at the decorator; every other field as in the [FastMCP table](fastmcp.md) | `<server>` is a name assigned `FastMCP(…)`, a parameter annotated `FastMCP`, or a name imported from a module that assigns one; for an imported server, the file that builds it decides the framework |
| `@<server>.tool` on a server built with the standalone `fastmcp` package | None here | Recorded once, as [`fastmcp`](fastmcp.md) | Each tool is emitted once, by the package of its server |
| A computed tool name (`name=NAME`) | None | — | The tool exists, but its name is unknown |

Not covered yet (follow-up): the low-level API (`Server.list_tools` / `call_tool`), `mcp_resource` / `mcp_prompt`, and MCP **client** calls.
