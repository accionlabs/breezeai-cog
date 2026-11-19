"""MCP server tools of Spring AI (``@McpTool`` / ``@Tool``) in Java and Kotlin:
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
"""

from __future__ import annotations

import pytest

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


JAVA_TOOLS = """
package demo;

import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.ai.chat.model.ToolContext;

public class WeatherTools {
    @Tool(name = "current_weather", description = "Current weather for a city")
    public String weather(@ToolParam(description = "City name") String city,
                          @ToolParam(description = "Units", required = false) String units,
                          ToolContext ctx) {
        return "";
    }

    @Tool(description = "Server time")
    public String now() { return ""; }
}
"""


POM_MCP = "<project><dependencies><dependency><artifactId>spring-ai-starter-mcp-server-webmvc</artifactId></dependency></dependencies></project>"


def test_spring_ai_tools_in_an_mcp_server(analyze_files) -> None:
    tools = _tools(
        analyze_files({"pom.xml": POM_MCP, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS})
    )
    assert set(tools) == {"current_weather", "now"}  # `now`: default name = method name
    w = tools["current_weather"]
    assert (w.framework, w.handler, w.description) == (
        "spring-ai-mcp",
        "weather",
        "Current weather for a city",
    )
    assert _params(w) == [
        ("city", "String", True, None, "City name"),
        ("units", "String", False, None, "Units"),
    ]


def test_spring_ai_tool_without_an_mcp_server_is_a_chat_tool(analyze_files) -> None:
    pom = "<project><dependencies><dependency><artifactId>spring-ai-starter-model-openai</artifactId></dependency></dependencies></project>"
    assert (
        _tools(analyze_files({"pom.xml": pom, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}))
        == {}
    )


def test_mcp_tool_annotation_needs_no_build_evidence(analyze_files) -> None:
    src = """
package demo;

import org.springaicommunity.mcp.annotation.McpTool;
import org.springaicommunity.mcp.annotation.McpToolParam;

public class Calc {
    @McpTool(name = "add", description = "Add")
    public int add(@McpToolParam(description = "First", required = true) int a, int b) { return a + b; }
}
"""
    tools = _tools(analyze_files({"src/main/java/demo/Calc.java": src}))
    assert _params(tools["add"]) == [
        ("a", "int", True, None, "First"),
        ("b", "int", True, None, None),
    ]


def test_spring_ai_tool_only_in_the_mcp_server_module(analyze_files) -> None:
    files = {
        "pom.xml": "<project><modules><module>mcp-server</module></modules></project>",
        "mcp-server/pom.xml": POM_MCP,
        "mcp-server/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
        "chat-app/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
    }
    statements = analyze_files(files)
    paths = {s.path for s in statements if s.semanticType == "mcp_tool"}
    assert paths == {"mcp-server/src/main/java/demo/WeatherTools.java"}


POM_BOM_PARENT = """<project>
  <modules><module>chat</module></modules>
  <dependencyManagement><dependencies>
    <dependency><artifactId>spring-ai-starter-mcp-server-webmvc</artifactId></dependency>
  </dependencies></dependencyManagement>
  <!-- <dependency><artifactId>spring-ai-starter-mcp-server</artifactId></dependency> -->
</project>"""


def test_spring_ai_bom_entry_is_not_an_mcp_server(analyze_files) -> None:
    files = {
        "pom.xml": POM_BOM_PARENT,
        "chat/pom.xml": "<project><dependencies><dependency><artifactId>spring-ai-starter-model-openai</artifactId></dependency></dependencies></project>",
        "chat/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
    }
    assert _tools(analyze_files(files)) == {}


def test_spring_ai_gradle_platform_is_not_an_mcp_server(analyze_files) -> None:
    gradle = 'dependencies {\n  implementation platform("org.springframework.ai:spring-ai-mcp-server-bom:1.0.0")\n}\n'
    files = {"build.gradle": gradle, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}
    assert _tools(analyze_files(files)) == {}


def test_spring_ai_tool_needs_the_spring_import(analyze_files) -> None:
    other = JAVA_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool;", "import com.acme.Tool;"
    )
    files = {"pom.xml": POM_MCP, "src/main/java/demo/WeatherTools.java": other}
    assert _tools(analyze_files(files)) == {}
    fqn = (
        "package demo;\nimport org.springframework.ai.chat.model.ToolContext;\n"
        'public class T {\n  @org.springframework.ai.tool.annotation.Tool(description = "D")\n'
        '  public String now() { return ""; }\n}\n'
    )
    files = {"pom.xml": POM_MCP, "src/main/java/demo/T.java": fqn}
    assert set(_tools(analyze_files(files))) == {"now"}


KOTLIN_TOOLS = """
package demo

import org.springframework.ai.tool.annotation.Tool
import org.springframework.ai.tool.annotation.ToolParam
import org.springframework.ai.chat.model.ToolContext

class WeatherTools {
    @Tool(name = "current_weather", description = "Current weather for a city")
    fun weather(
        @ToolParam(description = "City name") city: String,
        @ToolParam(description = "Units", required = false) units: String,
        days: Int = 3,
        lang: String?,
        @ToolParam(description = "Region", required = true) region: String? = null,
        ctx: ToolContext,
    ): String = ""

    @Tool
    fun now(): String = ""
}
"""


GRADLE_KTS_MCP = """dependencies {
    implementation("org.springframework.ai:spring-ai-starter-mcp-server-webflux")
}
"""


def test_kotlin_spring_ai_tools_with_a_gradle_kts_starter(analyze_files) -> None:
    files = {
        "build.gradle.kts": GRADLE_KTS_MCP,
        "src/main/kotlin/demo/WeatherTools.kt": KOTLIN_TOOLS,
    }
    tools = _tools(analyze_files(files))
    assert set(tools) == {"current_weather", "now"}  # `now`: default name = function name
    w = tools["current_weather"]
    assert (w.framework, w.handler, w.handlerLine, w.description) == (
        "spring-ai-mcp",
        "weather",
        9,
        "Current weather for a city",
    )
    assert w.parentId and w.parentId.endswith("#WeatherTools#weather@9")
    assert _params(w) == [
        ("city", "String", True, None, "City name"),
        ("units", "String", False, None, "Units"),
        # Spring AI reads `required` from the annotation only: a default or a nullable type
        # does not make a parameter optional.
        ("days", "Int", True, "3", None),
        ("lang", "String?", True, None, None),
        ("region", "String?", True, "null", "Region"),
    ]
    assert tools["now"].description is None and _params(tools["now"]) == []


def test_kotlin_long_default_is_cut(analyze_files) -> None:
    src = KOTLIN_TOOLS.replace("days: Int = 3,", 'days: String = "' + "x" * 300 + '",')
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": src}
    days = next(
        p for p in _tools(analyze_files(files))["current_weather"].params if p.name == "days"
    )
    assert len(days.default) == 200 and days.default.endswith("…")


def test_java_spring_ai_tools_with_a_gradle_kts_starter(analyze_files) -> None:
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}
    assert set(_tools(analyze_files(files))) == {"current_weather", "now"}


def test_kotlin_spring_ai_tool_without_an_mcp_server_is_a_chat_tool(analyze_files) -> None:
    gradle = 'dependencies {\n    implementation("org.springframework.ai:spring-ai-starter-model-openai")\n}\n'
    files = {"build.gradle.kts": gradle, "src/main/kotlin/demo/WeatherTools.kt": KOTLIN_TOOLS}
    assert _tools(analyze_files(files)) == {}
    other = KOTLIN_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool\n", "import com.acme.Tool\n"
    )
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": other}
    assert _tools(analyze_files(files)) == {}  # not Spring AI's @Tool


def test_kotlin_mcp_tool_annotation_needs_no_build_evidence(analyze_files) -> None:
    src = """
package demo

import org.springaicommunity.mcp.annotation.McpTool
import org.springaicommunity.mcp.annotation.McpToolParam

class Calc {
    @McpTool(name = "add", description = "Add")
    fun add(@McpToolParam(description = "First") a: Int, @McpToolParam(required = false) b: Int): Int = a + b

    @McpTool
    suspend fun reset() {}
}
"""
    tools = _tools(analyze_files({"src/main/kotlin/demo/Calc.kt": src}))
    assert set(tools) == {"add", "reset"}
    assert tools["add"].framework == "spring-ai-mcp"
    assert _params(tools["add"]) == [
        ("a", "Int", True, None, "First"),
        ("b", "Int", False, None, None),
    ]
    assert tools["reset"].description is None


def test_kotlin_aliased_spring_ai_tool(analyze_files) -> None:
    aliased = (
        KOTLIN_TOOLS.replace(
            "import org.springframework.ai.tool.annotation.Tool\n",
            "import org.springframework.ai.tool.annotation.Tool as AiTool\n",
        )
        .replace("@Tool(", "@AiTool(")
        .replace("@Tool\n", "@AiTool\n")
    )
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": aliased}
    tools = _tools(analyze_files(files))
    assert set(tools) == {"current_weather", "now"}
    assert tools["current_weather"].framework == "spring-ai-mcp"
    assert _params(tools["current_weather"])[0] == ("city", "String", True, None, "City name")


def test_kotlin_aliased_tool_needs_the_starter_and_the_spring_class(analyze_files) -> None:
    aliased = (
        KOTLIN_TOOLS.replace(
            "import org.springframework.ai.tool.annotation.Tool\n",
            "import org.springframework.ai.tool.annotation.Tool as AiTool\n",
        )
        .replace("@Tool(", "@AiTool(")
        .replace("@Tool\n", "@AiTool\n")
    )
    assert _tools(analyze_files({"src/main/kotlin/demo/WeatherTools.kt": aliased})) == {}
    unrelated = (
        KOTLIN_TOOLS.replace(
            "import org.springframework.ai.tool.annotation.Tool\n",
            "import com.acme.Tool as AiTool\n",
        )
        .replace("@Tool(", "@AiTool(")
        .replace("@Tool\n", "@AiTool\n")
    )
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": unrelated}
    assert _tools(analyze_files(files)) == {}


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize(
    "build",
    [{"pom.xml": POM_MCP}, {"build.gradle.kts": GRADLE_KTS_MCP}],
    ids=["pom", "gradle-kts"],
)
def test_spring_ai_tools_with_a_build_file_starter_for_any_jobs(
    capture_mcp_tools, build, jobs
) -> None:
    files = {
        **build,
        "src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
        "src/main/kotlin/demo/ForecastTools.kt": KOTLIN_TOOLS.replace(
            "WeatherTools", "ForecastTools"
        ),
    }
    tools = capture_mcp_tools(files, jobs)
    assert set(tools) == {"current_weather", "now"}
    assert {t["framework"] for t in tools.values()} == {"spring-ai-mcp"}


@pytest.mark.parametrize("jobs", [1, 2])
def test_spring_ai_tools_without_the_starter_for_any_jobs(capture_mcp_tools, jobs) -> None:
    files = {"pom.xml": "<project/>", "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}
    assert capture_mcp_tools(files, jobs) == {}


JAVA_BOTH = """
package demo;

import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;
import org.springframework.ai.tool.annotation.Tool;

public class Mixed {
    @Tool(description = "Now")
    public String now() { return ""; }

    McpServerFeatures.SyncToolSpecification calc() {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("calc", "Calculator", "{}"), (exchange, args) -> null);
    }
}
"""

KOTLIN_BOTH = """
package demo

import io.modelcontextprotocol.server.McpServerFeatures
import io.modelcontextprotocol.spec.McpSchema
import org.springframework.ai.tool.annotation.Tool

class Mixed {
    @Tool(description = "Now")
    fun now(): String = ""

    fun calc() = McpServerFeatures.SyncToolSpecification(
        McpSchema.Tool("calc", "Calculator", "{}")
    ) { exchange, args -> null }
}
"""


@pytest.mark.parametrize(
    ("path", "source"),
    [("src/main/java/demo/Mixed.java", JAVA_BOTH), ("src/main/kotlin/demo/Mixed.kt", KOTLIN_BOTH)],
    ids=["java", "kotlin"],
)
def test_annotated_tool_and_tool_specification_in_one_file_are_each_emitted_once(
    analyze_files, path, source
) -> None:
    """The Spring AI package reads only the annotated method, the SDK package only the
    registration: neither tool is emitted twice."""
    statements = analyze_files({"pom.xml": POM_MCP, path: source})
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    assert sorted((t.endpoint, t.framework) for t in tools) == [
        ("calc", "java-mcp"),
        ("now", "spring-ai-mcp"),
    ]


# ── Resources and prompts (@McpResource / @McpPrompt) ─────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "spring-ai-mcp"
    return {s.endpoint: s for s in found}


def _p(s: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.description) for p in s.params or []]


JAVA_ENTRIES = """
package demo;

import org.springaicommunity.mcp.annotation.McpArg;
import org.springaicommunity.mcp.annotation.McpPrompt;
import org.springaicommunity.mcp.annotation.McpResource;

public class Entries {
    @McpResource(uri = "users://{id}/profile", name = "Profile", description = "A user's profile")
    public String profile(String id) { return id; }

    @McpResource(uri = "config://app")
    public String config() { return ""; }

    @McpResource(name = "nouri")
    public String noUri() { return ""; }

    @McpPrompt(name = "review", description = "Review code")
    public Object review(@McpArg(name = "code", description = "The code", required = true) String code,
                         String style, GetPromptRequest request) { return null; }

    @McpPrompt
    public Object greet() { return null; }
}
"""


def test_spring_ai_java_resources_and_prompts_with_no_tool(analyze_files) -> None:
    statements = analyze_files({"src/Entries.java": JAVA_ENTRIES})
    resources = _of(statements, "mcp_resource")
    assert set(resources) == {"users://{id}/profile", "config://app"}
    profile = resources["users://{id}/profile"]
    assert (profile.description, profile.handler, profile.handlerLine) == (
        "A user's profile",
        "profile",
        9,
    )
    assert _p(profile) == [("id", "string", True, None)]
    assert resources["config://app"].params == []
    prompts = _of(statements, "mcp_prompt")
    assert set(prompts) == {"review", "greet"}
    assert prompts["review"].description == "Review code"
    assert _p(prompts["review"]) == [("code", None, True, "The code"), ("style", None, False, None)]
    assert prompts["greet"].params == []


KOTLIN_ENTRIES = """
package demo

import org.springaicommunity.mcp.annotation.McpArg
import org.springaicommunity.mcp.annotation.McpPrompt
import org.springaicommunity.mcp.annotation.McpResource

class Entries {
    @McpResource(uri = "docs://{name}", description = "A doc")
    fun doc(name: String): String = name

    @McpPrompt(name = "explain")
    fun explain(@McpArg(description = "Topic", required = true) topic: String, ctx: McpSyncRequestContext): Any = TODO()
}
"""


def test_spring_ai_kotlin_resources_and_prompts_with_no_tool(analyze_files) -> None:
    statements = analyze_files({"src/Entries.kt": KOTLIN_ENTRIES})
    doc = _of(statements, "mcp_resource")["docs://{name}"]
    assert (doc.description, doc.handler, doc.handlerLine) == ("A doc", "doc", 9)
    explain = _of(statements, "mcp_prompt")["explain"]
    assert _p(explain) == [("topic", None, True, "Topic")]


def test_spring_ai_resource_look_alikes(analyze_files) -> None:
    src = """
package demo;

public class Other {
    @Resource(name = "dataSource")
    private Object ds;

    @Prompt("hello")
    public String hello() { return ""; }
}
"""
    statements = analyze_files({"src/Other.java": src})
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]
