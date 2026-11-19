"""MCP server tools of the C# SDK (``ModelContextProtocol``): one ``mcp_tool`` statement per tool.

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


CS_TOOLS = """
using System.ComponentModel;
using ModelContextProtocol.Server;

[McpServerToolType]
public static class EchoTools
{
    [McpServerTool(Name = "echo"), Description("Echoes the message")]
    public static string Echo([Description("The message")] string message, int times = 1,
                              string? prefix, CancellationToken ct = default) => message;

    /// <summary>
    /// Reverses the message.
    /// </summary>
    [McpServerTool]
    public static string Reverse(IMcpServer server, string message) => message;
}
"""


def test_csharp_sdk_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"src/EchoTools.cs": CS_TOOLS}))
    echo = tools["echo"]
    assert (echo.framework, echo.handler, echo.description) == (
        "csharp-mcp",
        "Echo",
        "Echoes the message",
    )
    assert _params(echo) == [
        ("message", "string", True, None, "The message"),
        ("times", "int", False, "1", None),
        ("prefix", "string?", True, None, None),  # nullable alone is still required
    ]
    reverse = tools["reverse"]  # no Name =: the SDK's derived name (snake_case)
    assert reverse.handler == "Reverse" and reverse.name == "reverse"
    assert reverse.description == "Reverses the message."
    assert _params(reverse) == [("message", "string", True, None, None)]


def test_csharp_tool_name_derived_like_the_sdk(analyze_files) -> None:
    src = """
using System.Threading.Tasks;
using ModelContextProtocol.Server;

[McpServerToolType]
public class Tools
{
    [McpServerTool] public static async Task<string> GetWeatherAsync(string city) => city;
    [McpServerTool] public static ValueTask PingAsync() => default;
    [McpServerTool] public static string ExportAsync() => "";
    [McpServerTool] public static string GetHTTPStatus2() => "";
    [McpServerTool] public static string Async() => "";
}
"""
    tools = _tools(analyze_files({"src/Tools.cs": src}))
    # `Async` dropped only for an async return type; acronyms grouped as System.Text.Json does
    assert set(tools) == {"get_weather", "ping", "export_async", "get_http_status2", "async"}
    assert tools["get_weather"].handler == "GetWeatherAsync"


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "csharp-mcp"
    return {s.endpoint: s for s in found}


CSHARP_ENTRIES = """
using System.ComponentModel;
using ModelContextProtocol.Server;

[McpServerResourceType]
public static class Resources
{
    [McpServerResource(UriTemplate = "test://direct/text", Name = "Direct"), Description("A direct resource")]
    public static string Direct() => "x";

    [McpServerResource(UriTemplate = "users://{id}", Name = "User")]
    public static string User(string id) => id;

    [McpServerResource, Description("Derived URI")]
    public static string Derived() => "x";
}

[McpServerPromptType]
public static class Prompts
{
    [McpServerPrompt(Name = "complex_prompt"), Description("A prompt with arguments")]
    public static string Complex([Description("Temperature")] int temperature, string? style = null) => "";

    [McpServerPrompt]
    public static string SimplePromptAsync() => "";
}
"""


def test_csharp_resources_and_prompts_with_no_tool(analyze_files) -> None:
    statements = analyze_files({"Entries.cs": CSHARP_ENTRIES})
    resources = _of(statements, "mcp_resource")
    assert set(resources) == {"test://direct/text", "users://{id}"}  # a derived URI is not read
    direct = resources["test://direct/text"]
    assert (direct.method, direct.description, direct.handler, direct.params) == (
        "RESOURCE",
        "A direct resource",
        "Direct",
        [],
    )
    assert [(p.name, p.type, p.required) for p in resources["users://{id}"].params or []] == [
        ("id", "string", True)
    ]
    prompts = _of(statements, "mcp_prompt")
    assert set(prompts) == {"complex_prompt", "simple_prompt_async"}
    complex_ = prompts["complex_prompt"]
    assert complex_.method == "PROMPT" and complex_.description == "A prompt with arguments"
    assert [(p.name, p.required, p.description) for p in complex_.params or []] == [
        ("temperature", True, "Temperature"),
        ("style", False, None),
    ]
