"""MCP server tools of ``mark3labs/mcp-go``: one ``mcp_tool`` statement per tool.

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


GO_SERVER = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    tool := mcp.NewTool("hello",
        mcp.WithDescription("Say hello"),
        mcp.WithString("name", mcp.Required(), mcp.Description("Who to greet")),
        mcp.WithNumber("times", mcp.DefaultNumber(1)),
    )
    s.AddTool(tool, helloHandler)
    s.AddTool(mcp.NewTool("ping"), func(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
        return nil, nil
    })
}

func helloHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""


def test_go_mcp_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"main.go": GO_SERVER}))
    hello = tools["hello"]
    assert (hello.framework, hello.description, hello.handler) == (
        "mcp-go",
        "Say hello",
        "helloHandler",
    )
    assert hello.handlerLine == 22
    assert _params(hello) == [
        ("name", "string", True, None, "Who to greet"),
        ("times", "number", False, "1", None),
    ]
    ping = tools["ping"]
    assert ping.description is None and ping.params == [] and ping.handlerLine == 17


def test_go_reused_tool_variable_keeps_each_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var shared = mcp.NewTool("shared")

func regA(s *Server) {
    tool := mcp.NewTool("a")
    s.AddTool(tool, handleA)
}

func regB(s *Server) {
    tool := mcp.NewTool("b")
    s.AddTool(tool, handleB)
    tool = mcp.NewTool("c")
    s.AddTool(tool, handleC)
    s.AddTool(shared, handleShared)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "a": "handleA",
        "b": "handleB",
        "c": "handleC",
        "shared": "handleShared",
    }


def test_go_typed_tool_handler_is_looked_through(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddTool(mcp.NewTool("typed"), mcp.NewTypedToolHandler(typedHandler))
    s.AddTool(mcp.NewTool("other"), server.NewTypedToolHandler(typedHandler))
}

func typedHandler(ctx context.Context, r mcp.CallToolRequest, args Args) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    for name in ("typed", "other"):
        assert (tools[name].handler, tools[name].handlerLine) == ("typedHandler", 15), name


def test_go_options_that_cannot_be_read_leave_params_unknown(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

func reg(s *Server) {
    s.AddTool(mcp.NewTool("helper", mcp.WithString("q"), commonOpts()), h)
    s.AddTool(mcp.NewTool("raw", mcp.WithRawInputSchema(schema)), h)
    s.AddTool(mcp.NewTool("known", mcp.WithString("q"), mcp.WithReadOnlyHintAnnotation(true)), h)
    s.AddTool(mcp.NewTool("titled", mcp.WithTitleAnnotation("T"), mcp.WithOutputSchema[Out]()), h)
    s.AddTool(mcp.NewTool("newer", mcp.WithString("q"), mcp.WithStringEnum("mode", "a", "b")), h)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["helper"].params is None and tools["raw"].params is None
    assert _params(tools["known"]) == [("q", "string", False, None, None)]
    # An option the detector does not know may add a parameter: unknown, not a partial list.
    assert tools["newer"].params is None
    assert tools["titled"].params == []


def test_go_package_tool_var_is_not_paired_through_a_shadowing_local(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func other(s *Server) {
    tool := mcp.NewTool("other")
    s.AddTool(tool, otherHandler)
}

func pkg(s *Server) {
    s.AddTool(tool, pkgHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["other"].handler == "otherHandler"
    assert tools["pkg_level"].handler == "pkgHandler"


def test_go_package_tool_var_registered_twice_has_no_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func a(s *Server) { s.AddTool(tool, aHandler) }

func b(s *Server) { s.AddTool(tool, bHandler) }
"""
    assert _tools(analyze_files({"main.go": src}))["pkg_level"].handler is None


def test_go_reassigned_tool_var_stops_at_the_next_assignment(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func reg(s *server.MCPServer) {
    t := mcp.NewTool("first")
    s.AddTools(server.ServerTool{Tool: t, Handler: firstHandler})
    t = mcp.NewTool("second")
    s.AddTool(t, secondHandler)
    u := mcp.NewTool("unregistered")
    u = mcp.NewTool("third")
    s.AddTool(u, thirdHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "first": "firstHandler",
        "second": "secondHandler",
        "unregistered": None,
        "third": "thirdHandler",
    }


def test_go_server_tool_literal_gives_the_handler(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    srv "github.com/mark3labs/mcp-go/server"
)

func reg(s *srv.MCPServer) {
    tools := []srv.ServerTool{
        {Tool: mcp.NewTool("listed"), Handler: listedHandler},
    }
    s.AddTools(
        srv.ServerTool{Tool: mcp.NewTool("inline"), Handler: inlineHandler},
        &srv.ServerTool{Tool: mcp.NewTool("pointer"), Handler: pointerHandler},
    )
    s.AddTools(tools...)
}

func inlineHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["inline"].handler, tools["inline"].handlerLine) == ("inlineHandler", 20)
    assert tools["pointer"].handler == "pointerHandler"
    assert tools["listed"].handler == "listedHandler"  # element of a []ServerTool literal


# ── Resources and prompts ─────────────────────────────────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "mcp-go"
    return {s.endpoint: s for s in found}


MCPGO_RESOURCES = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddResource(mcp.NewResource("docs://readme", "README",
        mcp.WithResourceDescription("The README"), mcp.WithMIMEType("text/markdown")), readmeHandler)
    tpl := mcp.NewResourceTemplate("users://{id}/profile", "User",
        mcp.WithTemplateDescription("A user"))
    s.AddResourceTemplate(tpl, userHandler)
    s.AddResource(mcp.NewResource("odd://{literal}", "Odd"), readmeHandler)
    s.AddResources(server.ServerResource{Resource: mcp.NewResource("cfg://app", "Cfg"), Handler: cfgHandler})
    s.AddResource(mcp.NewResource(uri, "computed"), readmeHandler)
}

func readmeHandler() {}
func userHandler() {}
func cfgHandler() {}
"""


def test_mcpgo_resources_with_no_tool(analyze_files) -> None:
    resources = _of(analyze_files({"main.go": MCPGO_RESOURCES}), "mcp_resource")
    assert set(resources) == {
        "docs://readme",
        "users://{id}/profile",
        "odd://{literal}",
        "cfg://app",
    }
    readme = resources["docs://readme"]
    assert (
        readme.method,
        readme.description,
        readme.handler,
        readme.handlerLine,
        readme.params,
    ) == ("RESOURCE", "The README", "readmeHandler", 21, [])
    user = resources["users://{id}/profile"]
    assert (user.description, user.handler, user.handlerLine) == ("A user", "userHandler", 22)
    assert _params(user) == [("id", "string", True, None, None)]
    assert resources["odd://{literal}"].params == []  # a fixed URI has no variables
    assert resources["cfg://app"].handler == "cfgHandler"


MCPGO_PROMPTS = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddPrompt(mcp.NewPrompt("review",
        mcp.WithPromptDescription("Review code"),
        mcp.WithArgument("code", mcp.ArgumentDescription("The code"), mcp.RequiredArgument()),
        mcp.WithArgument("style"),
    ), reviewHandler)
    s.AddPrompt(mcp.NewPrompt("simple"), func() {})
    s.AddPrompt(mcp.NewPrompt("opts", extraOpts...), reviewHandler)
}

func reviewHandler() {}
"""


def test_mcpgo_prompts_with_no_tool(analyze_files) -> None:
    prompts = _of(analyze_files({"main.go": MCPGO_PROMPTS}), "mcp_prompt")
    assert set(prompts) == {"review", "simple", "opts"}
    review = prompts["review"]
    assert (review.method, review.description, review.handler, review.handlerLine) == (
        "PROMPT",
        "Review code",
        "reviewHandler",
        20,
    )
    assert _params(review) == [
        ("code", None, True, None, "The code"),
        ("style", None, False, None, None),
    ]
    assert prompts["simple"].params == [] and prompts["simple"].handlerLine == 16
    assert prompts["opts"].params is None  # an option the reader cannot see


def test_mcpgo_resource_look_alike_package(analyze_files) -> None:
    src = """
package main

import "example.com/other/mcp"

func main() {
    s.AddResource(mcp.NewResource("docs://x", "x"), h)
    s.AddPrompt(mcp.NewPrompt("p"), h)
}
"""
    statements = analyze_files({"main.go": src})
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]


def test_go_handler_line_is_never_a_same_named_method(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddTool(mcp.NewTool("hello"), helloHandler)
    s.AddTool(mcp.NewTool("method_only"), lone)
}

func helloHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}

func (h *H) helloHandler() {}

func (h *H) lone() {}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["hello"].handler, tools["hello"].handlerLine) == ("helloHandler", 15)
    assert (tools["method_only"].handler, tools["method_only"].handlerLine) == ("lone", None)
