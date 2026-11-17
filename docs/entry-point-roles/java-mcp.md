# MCP Java SDK — entry-point roles

Parser: `src/breezeai_cog/parsers/java_mcp/tools.py` (additive detector); the JSON-schema reader is in `parsers/jvm_mcp_common.py`, shared with [`kotlin-mcp`](kotlin-mcp.md) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_java_mcp.py`

A tool specification of the MCP Java SDK (`io.modelcontextprotocol`), written in Java. The same construct written in Kotlin is in the [Kotlin table](kotlin-mcp.md), with the same `framework: java-mcp`. The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

| Construct | Role | Record | Why |
|---|---|---|---|
| Java `new McpServerFeatures.SyncToolSpecification(tool, handler)` / `AsyncToolSpecification`, or their `builder().tool(…).callHandler(…).build()` (MCP Java SDK, `io.modelcontextprotocol`) | Binding | `mcp_tool`, `framework: java-mcp`, at the constructor / `build()` call; the tool is `new McpSchema.Tool(name, description, schemaJson)` or `McpSchema.Tool.builder().name(…).description(…).inputSchema(…).build()`, inline or a local variable declared once in the file; `description` only from a literal (another `Tool` constructor overload: none); `params` from a JSON-schema string literal, else empty (unknown); `handler` = the method of a method reference (`this::calc`), a lambda gives only its line | A tool specification is server-side by definition |
| A Spring AI `@McpTool` / `@Tool` method in the same file | None here | Recorded once, as [`spring-ai-mcp`](spring-ai-mcp.md) | Each tool is emitted once, by the package of its construct |
| A computed tool name (`new McpSchema.Tool(NAME, …)`, `.name(computed())`) | None | — | The tool exists, but its name is unknown |
| A `…ToolSpecification` in a file that does not use `io.modelcontextprotocol` | None | — | Not the SDK's class |

Not covered yet (follow-up): the `McpServer.sync(…).tool(…)` / `toolCall(…)` shortcuts, `mcp_resource` / `mcp_prompt`, and MCP **client** calls.
