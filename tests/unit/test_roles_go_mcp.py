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


def test_go_sdk_tool_variable_with_any_other_use_is_not_read(analyze_files) -> None:
    """A tool variable is read only when the function uses it as nothing but the ``AddTool``
    argument: anything that could change it, or let something else change it, drops it."""
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func register(srv *mcp.Server) {
	twice := &mcp.Tool{Name: "twice"}
	srv.AddTool(twice, h)
	mcp.AddTool(srv, twice, g)

	passed := &mcp.Tool{Name: "passed"}
	decorate(passed)
	srv.AddTool(passed, h)

	aliased := &mcp.Tool{Name: "aliased"}
	p := aliased
	p.Name = "renamed"
	srv.AddTool(aliased, h)

	assignedTo := &mcp.Tool{Name: "assignedTo"}
	var q *mcp.Tool
	q = assignedTo
	srv.AddTool(assignedTo, h)

	deref := &mcp.Tool{Name: "deref"}
	*deref = mcp.Tool{Name: "other"}
	srv.AddTool(deref, h)

	read := &mcp.Tool{Name: "read"}
	log.Println(read.Name)
	srv.AddTool(read, h)
}
"""
    tools = [s for s in analyze_files({"tools.go": src}) if s.semanticType == "mcp_tool"]
    assert sorted(t.endpoint for t in tools) == ["twice", "twice"]  # two registrations


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


# ── Resources and prompts ─────────────────────────────────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "go-mcp"
    return {s.endpoint: s for s in found}


GO_SDK_RESOURCES = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "res"}, nil)
	server.AddResource(&mcp.Resource{Name: "info", URI: "embedded:info", Description: "Info"}, readInfo)
	tpl := &mcp.ResourceTemplate{Name: "user", URITemplate: "users://{id}"}
	server.AddResourceTemplate(tpl, readUser)
	server.AddResourceTemplate(&mcp.ResourceTemplate{URITemplate: "http://x/~{+path}"}, readUser)
	server.AddResource(&mcp.Resource{URI: uri}, readInfo)
}

func readInfo() {}
func readUser() {}
"""


def test_go_sdk_resources_with_no_tool(analyze_files) -> None:
    resources = _of(analyze_files({"main.go": GO_SDK_RESOURCES}), "mcp_resource")
    assert set(resources) == {"embedded:info", "users://{id}", "http://x/~{+path}"}
    info = resources["embedded:info"]
    assert (info.method, info.description, info.handler, info.handlerLine, info.params) == (
        "RESOURCE",
        "Info",
        "readInfo",
        15,
        [],
    )
    user = resources["users://{id}"]
    assert user.handler == "readUser" and _params(user) == [("id", "string", True, None, None)]
    assert resources["http://x/~{+path}"].params is None


GO_SDK_PROMPTS = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func run(server *mcp.Server) {
	server.AddPrompt(&mcp.Prompt{Name: "greet"}, greet)
	server.AddPrompt(&mcp.Prompt{
		Name:        "review",
		Description: "Review code",
		Arguments: []*mcp.PromptArgument{
			{Name: "code", Description: "The code", Required: true},
			&mcp.PromptArgument{Name: "style"},
		},
	}, func() {})
	server.AddPrompt(&mcp.Prompt{Name: "dyn", Arguments: args}, greet)
}

func greet() {}
"""


def test_go_sdk_prompts_with_no_tool(analyze_files) -> None:
    prompts = _of(analyze_files({"main.go": GO_SDK_PROMPTS}), "mcp_prompt")
    assert set(prompts) == {"greet", "review", "dyn"}
    assert prompts["greet"].params == [] and prompts["greet"].handlerLine == 19
    review = prompts["review"]
    assert (review.description, review.handler, review.handlerLine) == ("Review code", None, 15)
    assert _params(review) == [
        ("code", None, True, None, "The code"),
        ("style", None, False, None, None),
    ]
    assert prompts["dyn"].params is None


def test_go_sdk_resource_on_a_non_server_is_not_read(analyze_files) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func run(registry *Registry) {
	registry.AddResource(&mcp.Resource{URI: "x://y"}, h)
	registry.AddPrompt(&mcp.Prompt{Name: "p"}, h)
}
"""
    statements = analyze_files({"main.go": src})
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]


def test_go_sdk_resource_variable_read_only_when_its_sole_use_is_the_registration(
    analyze_files,
) -> None:
    src = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func run(server *mcp.Server) {
	kept := &mcp.Resource{URI: "kept://x", Name: "kept"}
	server.AddResource(kept, h)
	changed := &mcp.Resource{URI: "changed://x", Name: "changed"}
	changed.URI = other
	server.AddResource(changed, h)
	p := &mcp.Prompt{Name: "aliased"}
	alias := p
	server.AddPrompt(p, h)
	use(alias)
}

func h() {}
"""
    statements = analyze_files({"main.go": src})
    assert set(_of(statements, "mcp_resource")) == {"kept://x"}
    assert not _of(statements, "mcp_prompt")
