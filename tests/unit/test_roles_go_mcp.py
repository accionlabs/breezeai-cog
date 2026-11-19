"""MCP server tools of the official Go SDK (``github.com/modelcontextprotocol/go-sdk``): one
``mcp_tool`` statement per ``AddTool`` registration.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
"""

from __future__ import annotations

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


GO_SDK = """
package main

import (
	"context"

	"github.com/google/jsonschema-go/jsonschema"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type GreetArgs struct {
	Name   string   `json:"name" jsonschema:"the person to greet"`
	Times  *int     `json:"times,omitempty"`
	Tags   []string `json:"tags,omitzero"`
	Score  float64
	Extra  map[string]any `json:"extra"`
	When   Clock          `json:"when"`
}

func SayHi(ctx context.Context, req *mcp.CallToolRequest, args GreetArgs) (*mcp.CallToolResult, any, error) {
	return nil, nil, nil
}

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "greeter"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "greet", Description: "say hi"}, SayHi)
	server.AddTool(&mcp.Tool{
		Name:        "raw",
		Description: "Raw schema",
		InputSchema: &jsonschema.Schema{
			Type: "object",
			Properties: map[string]*jsonschema.Schema{
				"q": {Type: "string", Description: "Query"},
				"n": {Type: "integer"},
			},
			Required: []string{"q"},
		},
	}, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: "inline"}, func(ctx context.Context, req *mcp.CallToolRequest, in struct {
		City string `json:"city"`
	}) (*mcp.CallToolResult, any, error) {
		return nil, nil, nil
	})
	mcp.AddTool(server, &mcp.Tool{Name: name}, SayHi)
	server.AddTool(&mcp.Tool{Name: "opaque", InputSchema: json.RawMessage(`{}`)}, h.Handle)
}
"""


def test_go_sdk_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"main.go": GO_SDK}))
    assert set(tools) == {"greet", "raw", "inline", "opaque"}  # a computed name is not guessed
    greet = tools["greet"]
    assert (greet.framework, greet.description, greet.handler, greet.handlerLine) == (
        "go-mcp",
        "say hi",
        "SayHi",
        20,
    )
    assert greet.startLine == 26 and greet.nodeType == "call_expression"
    assert _params(greet) == [
        ("name", "string", True, None, "the person to greet"),
        ("times", "integer", False, None, None),
        ("tags", "array", False, None, None),
        ("Score", "number", True, None, None),  # no json tag: the field name, required
        ("extra", "object", True, None, None),
        ("when", None, True, None, None),  # a named type: its JSON type is not read
    ]
    raw = tools["raw"]
    assert raw.description == "Raw schema" and raw.handlerLine == 38  # the literal's line
    assert _params(raw) == [
        ("q", "string", True, None, "Query"),
        ("n", "integer", False, None, None),
    ]
    assert _params(tools["inline"]) == [("city", "string", True, None, None)]
    opaque = tools["opaque"]
    assert opaque.params is None and opaque.handler == "h.Handle"


def test_go_sdk_input_struct_the_reader_cannot_follow_leaves_params_unknown(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

type Skip struct {
	Secret string `json:"-"`
}
type Hidden struct {
	visible string
}
type Embeds struct {
	Base
	Name string
}

func main() {
	s := mcp.NewServer(nil, nil)
	mcp.AddTool(s, &mcp.Tool{Name: "skip"}, func(ctx context.Context, r *mcp.CallToolRequest, in Skip) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "hidden"}, func(ctx context.Context, r *mcp.CallToolRequest, in Hidden) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "embeds"}, func(ctx context.Context, r *mcp.CallToolRequest, in Embeds) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool(s, &mcp.Tool{Name: "elsewhere"}, other.Handle)
	mcp.AddTool(s, &mcp.Tool{Name: "imported"}, func(ctx context.Context, r *mcp.CallToolRequest, in pkg.Args) (*mcp.CallToolResult, any, error) { return nil, nil, nil })
	mcp.AddTool[Skip, any](s, &mcp.Tool{Name: "explicit"}, h)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert set(tools) == {"skip", "hidden", "embeds", "elsewhere", "imported", "explicit"}
    assert all(t.params is None for t in tools.values())


def test_go_sdk_tool_variable_read_only_when_nothing_changes_it(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func register(srv *mcp.Server) {
	kept := &mcp.Tool{Name: "kept", Description: "Kept"}
	srv.AddTool(kept, handleKept)

	value := mcp.Tool{Name: "value"}
	srv.AddTool(&value, handleKept)

	renamed := &mcp.Tool{Name: "before"}
	renamed.Name = "after"
	srv.AddTool(renamed, handleKept)

	reassigned := &mcp.Tool{Name: "first"}
	reassigned = &mcp.Tool{Name: "second"}
	srv.AddTool(reassigned, handleKept)

	closure := &mcp.Tool{Name: "closure"}
	func() { closure.Description = "changed" }()
	srv.AddTool(closure, handleKept)
}

func handleKept(ctx context.Context, r *mcp.CallToolRequest) (*mcp.CallToolResult, error) { return nil, nil }
"""
    tools = _tools(analyze_files({"tools.go": src}))
    assert set(tools) == {"kept", "value"}
    assert (tools["kept"].handler, tools["kept"].handlerLine, tools["kept"].params) == (
        "handleKept",
        26,
        None,  # `Server.AddTool` without an input schema: unknown
    )


def test_go_sdk_add_tool_on_an_object_not_proven_a_server(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

var registry = NewRegistry()

func main() {
	registry.AddTool(&mcp.Tool{Name: "registry"}, h)
	other := makeServer()
	other.AddTool(&mcp.Tool{Name: "other"}, h)
}

func shadow(server *mcp.Server) {
	{
		server := NewFake()
		server.AddTool(&mcp.Tool{Name: "shadowed"}, h)
	}
	server.AddTool(&mcp.Tool{Name: "typed"}, h)
}
"""
    assert set(_tools(analyze_files({"main.go": src}))) == {"typed"}


def test_go_sdk_look_alikes_are_not_tools(analyze_files) -> None:
    files = {
        # another module whose package is also called `mcp`
        "other.go": """
package main

import "github.com/acme/agents/mcp"

func main() {
	s := mcp.NewServer(nil, nil)
	mcp.AddTool(s, &mcp.Tool{Name: "acme"}, h)
	s.AddTool(&mcp.Tool{Name: "acme2"}, h)
}
""",
    }
    assert _tools(analyze_files(files)) == {}


def test_mark3labs_file_is_read_once_by_its_own_detector(analyze_files) -> None:
    """A mark3labs/mcp-go server gives only ``mcp-go`` records, one per tool."""
    src = """
package main

import (
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	s := server.NewMCPServer("demo", "1.0.0")
	s.AddTool(mcp.NewTool("hello", mcp.WithDescription("Hi")), helloHandler)
	s.AddTool(mcp.Tool{Name: "struct"}, helloHandler)
}
"""
    tools = [s for s in analyze_files({"main.go": src}) if s.semanticType == "mcp_tool"]
    assert [(t.endpoint, t.framework) for t in tools] == [("hello", "mcp-go")]
