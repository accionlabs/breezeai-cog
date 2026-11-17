# Spring AI MCP — entry-point roles

Parsers: `src/breezeai_cog/parsers/java_springai/tools.py`, `kotlin_springai/tools.py` (additive detectors); the `spring-ai.mcp-server` fact and the parts both languages share are in `parsers/jvm_mcp_common.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_spring_ai_mcp.py`

A tool a Spring AI MCP server declares with an annotation, in Java or Kotlin. The fields of an `mcp_tool` record, how `params` is read and limited, and what an incremental sync does are the same for every MCP framework: [entry-point contract, MCP tools](../architecture/entry-points.md#mcp-tools).

Spring AI's `@Tool` is also how a chat client declares a function, so it is a tool only in a module **proven an MCP server**: one whose build file declares a Spring AI MCP server starter. `build.gradle.kts` / `settings.gradle.kts` are scanned as config files (Gradle Kotlin DSL), so the starter is found there too.

| Construct | Role | Record | Why |
|---|---|---|---|
| Java `@McpTool(name, description)` with `@McpToolParam(description, required)` (Spring AI MCP annotations) | Binding | `mcp_tool`, `framework: spring-ai-mcp`, at the annotation; `endpoint` = `name`, else the method name | MCP annotations by definition |
| Java `@Tool(name, description)` with `@ToolParam` (Spring AI) | Binding — **only** in a module whose build file (`pom.xml`, `build.gradle[.kts]` in the file's folder or an ancestor) declares a Spring AI MCP server starter as a dependency (a `<dependencyManagement>` entry or a Gradle `platform(…)` BOM does not count), and only for Spring AI's `@Tool` (imported from `org.springframework.ai.tool.annotation`, or fully qualified) | as above; `ToolContext` and MCP exchange / context parameters skipped | Without an MCP server, `@Tool` is a chat-client function |
| Kotlin `@McpTool` / Spring AI `@Tool` on a `fun`, with `@McpToolParam` / `@ToolParam` | Binding — the Java rules above, Kotlin syntax: `@Tool` only in a module whose build file declares an MCP server starter, and only Spring AI's | `mcp_tool`, `framework: spring-ai-mcp`, at the annotation; `endpoint` = `name`, else the function name; `required` from the annotation only, true by default (Kotlin nullability and a default value do not reach Spring AI's schema), `default` = the default's source text (200 characters at most) | As for Java |
| Kotlin import aliases (`import org.springframework.ai.tool.annotation.Tool as AiTool`, …) | — | The alias counts exactly as the class it names (`@AiTool` = Spring AI's `@Tool`, same starter gate) when that class is in `org.springframework.ai.*` or `org.springaicommunity.mcp.*` | An alias of any other class (`import com.acme.Tool as AiTool`) is never an MCP type, and hides the name it takes |
| A tool specification of the MCP Java SDK in the same file as an annotated tool | None here | Recorded once, as [`java-mcp`](java-mcp.md) | Each tool is emitted once, by the package of its construct |
| A computed tool name (`name = NAME`) | None | — | The tool exists, but its name is unknown |
| Spring AI `@Tool` without an MCP server, a `@Tool` of another library | None | — | A chat-client function, not an MCP server tool |

Not covered yet (follow-up): `mcp_resource` / `mcp_prompt`, and MCP **client** calls.
