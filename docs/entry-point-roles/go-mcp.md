# MCP Go SDK — entry-point roles

Parser: `src/breezeai_cog/parsers/go_mcp/tools.py` (additive detector); the Go import-name and string helpers are in `parsers/go_mcp_common.py`, shared with [`mcp-go`](mcp-go.md) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_go_mcp.py`

A tool a Go MCP server registers with the official SDK, `github.com/modelcontextprotocol/go-sdk`. The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

| Construct | Role | Record | Why |
|---|---|---|---|
| `mcp.AddTool(server, &mcp.Tool{Name: …, Description: …}, handler)` (the SDK's generic function; also `mcp.AddTool[In, Out](…)`) | Binding | `mcp_tool`, `framework: go-mcp`, at the `AddTool` call; `endpoint` = `Name` (a string literal); `description` = `Description` when a literal; `params` from a literal `InputSchema` (row below), else from the input struct the SDK infers the schema from — the type argument `In`, or the handler's third parameter (a same-file function or a function literal) — declared in this file: one param per field, named by its `json` tag (else the field name), required unless `omitempty` / `omitzero`, described by its `jsonschema` tag, typed `string` / `integer` / `number` / `boolean` / `array` / `object` for a builtin, else no type; `handler` = a function name (its line when in this file), a method value's text (`kb.Create`), or a function literal's line | `mcp` is the SDK's package under any import name; its `AddTool` takes only a `*mcp.Server` |
| `<server>.AddTool(&mcp.Tool{…}, handler)` | Binding — only when `<server>` is a name whose nearest binding is `mcp.NewServer(…)` or a parameter typed `*mcp.Server` | as above; `params` only from a literal `InputSchema` (the method infers nothing) | A method of the same name on any other object is not the SDK's |
| `InputSchema: &jsonschema.Schema{Type: …, Properties: map[string]*jsonschema.Schema{"a": {Type: …, Description: …}}, Required: []string{…}}` | — | Read as a JSON schema (`properties` / `required`); a schema that is not such a literal (`json.RawMessage(…)`, a variable, `jsonschema.For[T]()`) → `params` `null` | |
| A tool in a variable: `t := &mcp.Tool{…}` passed as `t`, or `t := mcp.Tool{…}` passed as `&t` | Binding — only when nothing else in the function (a closure included) assigns `t` or one of its fields | as above | `t.Name = …` after the literal would change the name |
| An input struct with a field the reader cannot follow (`json:"-"`, an unexported field, an embedded struct) or declared in another file / package; `map[string]any` | — | `params` `null` (unknown) | Never a partial list |
| A computed tool name (`Name: name`) | None | — | The tool exists, but its name is unknown |
| `mcp.Tool{…}` / `AddTool` of another module whose package is also named `mcp` (`mark3labs/mcp-go` is read by [`mcp-go`](mcp-go.md)) | None here | — | Each tool is emitted once, by the package of its SDK |

Not covered yet (follow-up): a tool registered on a server held in a struct field (`s.server.AddTool(…)`), `mcp_resource` / `mcp_prompt`, and MCP **client** calls.
