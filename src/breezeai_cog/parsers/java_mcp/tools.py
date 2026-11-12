"""Additive detector: tools of a Java MCP server built on Spring AI.

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    public String weather(@McpToolParam(description = "City") String city) { … }

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    public String weather(@ToolParam(description = "City", required = false) String city, ToolContext ctx)

* ``@McpTool`` / ``@McpToolParam`` (Spring AI MCP annotations) are MCP tools by definition.
* ``@Tool`` / ``@ToolParam`` are Spring AI's general function-calling annotations: a chat client
  uses them too. They count only in a repo whose build declares a Spring AI MCP **server**
  dependency (fact :data:`FACT`, read from ``pom.xml`` / ``build.gradle[.kts]``); otherwise
  nothing is emitted.
* ``endpoint`` — ``name =`` when a literal, else the method name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation (required by
  default); ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import (
    DetectContext,
    Detection,
    Detector,
    FactSpec,
    Gate,
    index_fact,
    register_detector,
    register_fact,
)
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "spring-ai-mcp"
FACT = "spring-ai.mcp-server"
_SERVER_DEPENDENCIES = (
    b"spring-ai-starter-mcp-server",
    b"spring-ai-mcp-server",
    b"spring-ai-mcp-server-spring-boot-starter",
)
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_INJECTED = frozenset(
    {
        "ToolContext",
        "McpSyncServerExchange",
        "McpAsyncServerExchange",
        "McpSyncRequestContext",
        "McpAsyncRequestContext",
        "McpTransportContext",
    }
)


def _collect(root: Any, source: bytes, rel: str, repo_root: Path | None) -> Any:
    if rel.rsplit("/", 1)[-1] in _BUILD_FILES and any(d in source for d in _SERVER_DEPENDENCIES):
        return True
    return None


def _resolve(values: list[Any], index: object) -> bool | None:
    return True if any(values) else None


register_fact(
    FactSpec(
        name=FACT,
        language="java",
        gate=(b"mcp-server",),
        collect=_collect,
        resolve=_resolve,
        sources=(".xml", ".gradle", ".kts"),
        grammar=None,
    )
)


def _string(node: Node | None, source: bytes) -> str | None:
    """A Java string literal or text block (``+`` concatenation of literals allowed)."""
    if node is None:
        return None
    if node.type == "binary_expression":
        parts = [_string(c, source) for c in node.named_children]
        return (
            "".join(p for p in parts if p is not None)
            if all(p is not None for p in parts)
            else None
        )
    if node.type != "string_literal":
        return None
    text = node_text(node, source)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]  # escapes kept as written


def _annotations(node: Node, source: bytes) -> dict[str, Node]:
    """Annotations in ``node``'s modifiers, by simple name."""
    out: dict[str, Node] = {}
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    for a in mods.named_children if mods is not None else []:
        if a.type in ("annotation", "marker_annotation"):
            name = a.child_by_field_name("name")
            if name is not None:
                out[node_text(name, source).rsplit(".", 1)[-1]] = a
    return out


def _element(annotation: Node, key: str, source: bytes) -> Node | None:
    args = annotation.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        if a.type == "element_value_pair":
            k = a.child_by_field_name("key")
            if k is not None and node_text(k, source) == key:
                return a.child_by_field_name("value")
        elif key == "value" and a.type != "comment":
            return a  # a lone value: @ToolParam("…")
    return None


def _params(method: Node, source: bytes, param_annotation: str) -> list[ToolParam]:
    params = method.child_by_field_name("parameters")
    out: list[ToolParam] = []
    for p in params.named_children if params is not None else []:
        if p.type != "formal_parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        type_text = node_text(type_node, source)
        if type_text.split("<")[0].rsplit(".", 1)[-1] in _INJECTED:
            continue
        ann = _annotations(p, source).get(param_annotation)
        description = required = None
        if ann is not None:
            description = _string(
                _element(ann, "description", source) or _element(ann, "value", source), source
            )
            req = _element(ann, "required", source)
            required = node_text(req, source) != "false" if req is not None else None
        out.append(
            param(
                node_text(name_node, source),
                type_text,
                required=True if required is None else required,
                description=description,
            )
        )
    return out


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and bool(index_fact(dc.ctx.resolution_index, FACT))
    if not mcp_annotations and not plain:
        return None
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in _walk(dc.root):
        if method.type != "method_declaration":
            continue
        anns = _annotations(method, source)
        if "McpTool" in anns:
            ann, param_ann = anns["McpTool"], "McpToolParam"
        elif plain and "Tool" in anns:
            ann, param_ann = anns["Tool"], "ToolParam"
        else:
            continue
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = _string(named, source) if named is not None else method_name
        if not endpoint:
            continue
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=FRAMEWORK,
                handler=method_name,
                handler_line=line,
                description=_string(_element(ann, "description", source), source),
                params=_params(method, source, param_ann),
                parent=owner.id if owner is not None else None,
            )
        )
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="java-mcp",
        gate=Gate("java", order=30, guard=b"Tool", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
