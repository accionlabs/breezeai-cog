"""A sub-reader added inside an existing MCP detector (a low-level server, a builder shortcut, an
``addTools`` list read from a ``val``) never drops the tools the detector's other readers found
in the file: forced to fail, it costs only its own records, and the failure is logged."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

PYTHON = """
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
import mcp.types as types

mcp = FastMCP("demo")
server = Server("low")


@mcp.tool()
def search(query: str) -> str:
    return query


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name="lowlevel", inputSchema={})]
"""

TYPESCRIPT = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const mcp = new McpServer({ name: "demo", version: "1" });
mcp.tool("search", async () => ({ content: [] }));

const server = new Server({ name: "low", version: "1" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ name: "lowlevel", inputSchema: { type: "object", properties: {} } }],
}));
"""

JAVA = """
package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    McpServerFeatures.SyncToolSpecification search() {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("search", "Search", "{}"), (exchange, args) -> null);
    }

    void start(Object transport) {
        McpServer.sync(transport).tool(new McpSchema.Tool("shortcut", "S", "{}"), (e, a) -> null).build();
    }
}
"""

KOTLIN = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.server.McpServer
import io.modelcontextprotocol.spec.McpSchema

val listed = listOf(RegisteredTool(Tool(name = "listed")) { _ -> null })

fun Server.register(transport: Any) {
    addTool("search", "Search") { _ -> null }
    addTools(listed)
    McpServer.sync(transport).tool(McpSchema.Tool("shortcut", "S", "{}")) { _, _ -> null }.build()
}
"""

CASES = [
    ("python_mcp.tools", "detect_lowlevel", "srv.py", PYTHON, {"search", "lowlevel"}),
    (
        "typescript_mcp.tools",
        "detect_lowlevel",
        "src/index.ts",
        TYPESCRIPT,
        {"search", "lowlevel"},
    ),
    (
        "java_mcp.tools",
        "_shortcut_tools",
        "src/main/java/demo/App.java",
        JAVA,
        {"search", "shortcut"},
    ),
    (
        "kotlin_mcp.tools",
        "_shortcut_tools",
        "src/main/kotlin/demo/App.kt",
        KOTLIN,
        {"search", "listed", "shortcut"},
    ),
    (
        "kotlin_mcp.tools",
        "_registered_tools",
        "src/main/kotlin/demo/App.kt",
        KOTLIN,
        {"search", "listed", "shortcut"},
    ),
]


def _tool_names(statements) -> set[str | None]:
    return {s.endpoint for s in statements if s.semanticType == "mcp_tool"}


@pytest.mark.parametrize(("module", "reader", "path", "src", "everything"), CASES)
def test_a_failing_subreader_keeps_the_detectors_other_tools(
    analyze_files, monkeypatch, module, reader, path, src, everything
) -> None:
    assert _tool_names(analyze_files({path: src})) == everything

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(f"breezeai_cog.parsers.{module}.{reader}", boom)
    with capture_logs() as logs:
        kept = _tool_names(analyze_files({path: src}))
    assert "search" in kept and kept < everything  # only the failing reader's records go
    failed = [e for e in logs if e["event"] == "mcp.subreader.failed"]
    assert failed and failed[0]["reader"] == "boom" and failed[0]["error"] == "ValueError: boom"
    assert not [e for e in logs if e["event"] == "additive.detector.failed"]


@pytest.mark.parametrize(
    ("module", "reader", "path", "src", "everything"),
    [
        (
            "java_mcp.tools",
            "_sdk_entries",
            "src/main/java/demo/App.java",
            JAVA,
            {"search", "shortcut"},
        ),
        (
            "kotlin_mcp.tools",
            "_entries",
            "src/main/kotlin/demo/App.kt",
            KOTLIN,
            {"search", "listed", "shortcut"},
        ),
    ],
)
def test_a_failing_resource_and_prompt_reader_keeps_every_tool(
    analyze_files, monkeypatch, module, reader, path, src, everything
) -> None:
    """The resource / prompt readers run guarded too: forced to fail, the
    file's tools all stay."""

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(f"breezeai_cog.parsers.{module}.{reader}", boom)
    with capture_logs() as logs:
        assert _tool_names(analyze_files({path: src})) == everything
    assert [e for e in logs if e["event"] == "mcp.subreader.failed"]
    assert not [e for e in logs if e["event"] == "additive.detector.failed"]


SPRING_JAVA = """
package demo;

import org.springaicommunity.mcp.annotation.McpTool;
import org.springaicommunity.mcp.annotation.McpPrompt;
import org.springaicommunity.mcp.annotation.McpResource;

public class Entries {
    @McpTool(name = "search", description = "Search")
    public String search(String q) { return q; }

    @McpResource(uri = "config://app")
    public String config() { return ""; }

    @McpPrompt(name = "review")
    public Object review(String code) { return null; }
}
"""

SPRING_KOTLIN = """
package demo

import org.springaicommunity.mcp.annotation.McpTool
import org.springaicommunity.mcp.annotation.McpPrompt
import org.springaicommunity.mcp.annotation.McpResource

class Entries {
    @McpTool(name = "search", description = "Search")
    fun search(q: String): String = q

    @McpResource(uri = "config://app")
    fun config(): String = ""

    @McpPrompt(name = "review")
    fun review(code: String): Any = TODO()
}
"""

PYTHON_ENTRIES = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def search(query: str) -> str:
    return query


@mcp.resource("config://app")
def config() -> str:
    return ""


@mcp.prompt(name="review")
def review(code: str) -> str:
    return code
"""

TYPESCRIPT_ENTRIES = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const mcp = new McpServer({ name: "demo", version: "1" });
mcp.tool("search", async () => ({ content: [] }));
mcp.resource("config", "config://app", async () => ({ contents: [] }));
mcp.prompt("review", { code: z.string() }, async () => ({ messages: [] }));
"""

TYPESCRIPT_LOWLEVEL_ENTRIES = """
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  ListToolsRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListPromptsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server({ name: "demo", version: "1" }, { capabilities: {} });
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{ name: "search", inputSchema: { type: "object", properties: {} } }],
}));
server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
  resourceTemplates: [{ uriTemplate: "config://app", name: "config" }],
}));
server.setRequestHandler(ListPromptsRequestSchema, async () => ({
  prompts: [{ name: "review", arguments: [{ name: "code", required: true }] }],
}));
"""

CSHARP_ENTRIES = """
using ModelContextProtocol.Server;

[McpServerToolType]
public static class Entries
{
    [McpServerTool(Name = "search")]
    public static string Search(string q) => q;

    [McpServerResource(UriTemplate = "config://app")]
    public static string Config() => "";

    [McpServerPrompt(Name = "review")]
    public static string Review(string code) => code;
}
"""

#: (module, a function only the resource or prompt reader calls, path, source, kind it drops)
ENTRY_CASES = [
    ("java_springai.tools", "_annotated_entries", "src/Entries.java", SPRING_JAVA, None),
    ("kotlin_springai.tools", "_annotated_entries", "src/Entries.kt", SPRING_KOTLIN, None),
    ("python_mcp_common", "_resource", "srv.py", PYTHON_ENTRIES, "mcp_resource"),
    ("python_mcp_common", "prompt_params", "srv.py", PYTHON_ENTRIES, "mcp_prompt"),
    ("typescript_mcp.tools", "_resource_uri", "src/index.ts", TYPESCRIPT_ENTRIES, "mcp_resource"),
    ("typescript_mcp.tools", "prompt_params", "src/index.ts", TYPESCRIPT_ENTRIES, "mcp_prompt"),
    # the low-level reader: each list handler is read guarded, so one kind failing keeps the others
    (
        "typescript_mcp.lowlevel",
        "uri_template_params",
        "src/low.ts",
        TYPESCRIPT_LOWLEVEL_ENTRIES,
        "mcp_resource",
    ),
    (
        "typescript_mcp.lowlevel",
        "_prompt_arguments",
        "src/low.ts",
        TYPESCRIPT_LOWLEVEL_ENTRIES,
        "mcp_prompt",
    ),
    ("csharp_mcp.tools", "uri_template_params", "Entries.cs", CSHARP_ENTRIES, "mcp_resource"),
    ("csharp_mcp.tools", "prompt_params", "Entries.cs", CSHARP_ENTRIES, "mcp_prompt"),
]


@pytest.mark.parametrize(("module", "reader", "path", "src", "dropped"), ENTRY_CASES)
def test_a_failing_resource_or_prompt_read_in_the_tool_detector_keeps_its_tools(
    analyze_files, monkeypatch, module, reader, path, src, dropped
) -> None:
    """Resources and prompts read inside a tool detector (Spring AI, the Python decorators, the
    TypeScript ``McpServer`` methods and low-level list handlers, C# attributes) run guarded: forced to fail, the file's
    tools stay, and so does the other kind (``dropped`` None: the one reader reads both)."""
    kinds = ("mcp_tool", "mcp_resource", "mcp_prompt")

    def names(statements) -> dict[str, set[str | None]]:
        return {k: {s.endpoint for s in statements if s.semanticType == k} for k in kinds}

    everything = names(analyze_files({path: src}))
    assert everything == {
        "mcp_tool": {"search"},
        "mcp_resource": {"config://app"},
        "mcp_prompt": {"review"},
    }

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(f"breezeai_cog.parsers.{module}.{reader}", boom)
    with capture_logs() as logs:
        kept = names(analyze_files({path: src}))
    gone = {dropped} if dropped else {"mcp_resource", "mcp_prompt"}
    assert kept == {k: set() if k in gone else everything[k] for k in kinds}
    assert [e for e in logs if e["event"] == "mcp.subreader.failed"]
    assert not [e for e in logs if e["event"] == "additive.detector.failed"]


def test_a_parse_timeout_in_a_subreader_is_not_swallowed() -> None:
    """``guarded`` turns a sub-reader's failure into a warning, but not a ``ParserTimeout``: the
    file's parse budget is spent, and the file is handled like any parse timeout. Nothing
    raises it today; the re-raise keeps one raised later from becoming a partial record set."""
    from breezeai_cog.errors import ParserTimeout
    from breezeai_cog.parsers.mcp_common import guarded

    def timeout(*args):
        raise ParserTimeout("x.ts")

    with capture_logs() as logs, pytest.raises(ParserTimeout):
        guarded("x.ts", timeout, 1)
    assert not [e for e in logs if e["event"] == "mcp.subreader.failed"]
