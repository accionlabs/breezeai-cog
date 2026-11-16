"""Additive detector: tools of a Java MCP server built on Spring AI.

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    public String weather(@McpToolParam(description = "City") String city) { … }

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    public String weather(@ToolParam(description = "City", required = false) String city, ToolContext ctx)

* ``@McpTool`` / ``@McpToolParam`` (Spring AI MCP annotations) are MCP tools by definition.
* ``@Tool`` / ``@ToolParam`` are Spring AI's general function-calling annotations: a chat client
  uses them too. They count only in a module whose build file declares a Spring AI MCP
  **server** dependency (:func:`~..jvm_mcp_common.in_mcp_server_module`); otherwise nothing is
  emitted, so a chat-client module next to an MCP server module stays out. ``@Tool`` must also
  be Spring AI's: imported from ``org.springframework.ai.tool.annotation`` or written fully
  qualified.
* ``endpoint`` — ``name =`` when a literal, else the method name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation (required by
  default); ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.

Only annotated methods are read here. A tool registered through the MCP Java SDK
(``McpServerFeatures.SyncToolSpecification``) belongs to ``java_mcp``, also in a Spring
application.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    INJECTED,
    SPRING_AI,
    SPRING_TOOL_FQN,
    in_mcp_server_module,
    java_string,
    walk,
)
from ..mcp_common import param, tool_emit
from ..treesitter import node_text


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


def _params(method: Node, source: bytes, param_annotation: str) -> list[Parameter]:
    params = method.child_by_field_name("parameters")
    out: list[Parameter] = []
    for p in params.named_children if params is not None else []:
        if p.type != "formal_parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        type_text = node_text(type_node, source)
        if type_text.split("<")[0].rsplit(".", 1)[-1] in INJECTED:
            continue
        ann = _annotations(p, source).get(param_annotation)
        description = required = None
        if ann is not None:
            description = java_string(
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


#: ``@Tool`` is Spring AI's only when imported from its package.
_SPRING_TOOL_IMPORT = re.compile(
    rb"import\s+org\.springframework\.ai\.tool\.annotation\.(?:Tool|\*)\s*;"
)


def _spring_tool(ann: Node, source: bytes, imported: bool) -> bool:
    name = ann.child_by_field_name("name")
    text = node_text(name, source) if name is not None else ""
    return text == SPRING_TOOL_FQN or (imported and text == "Tool")


def _annotated_tools(dc: DetectContext) -> list[Emit]:
    """Spring AI ``@McpTool`` / ``@Tool`` methods."""
    source = dc.source
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and in_mcp_server_module(
        dc.record.path, dc.ctx.resolution_index
    )
    if not mcp_annotations and not plain:
        return []
    imported = bool(_SPRING_TOOL_IMPORT.search(source))
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in walk(dc.root):
        if method.type != "method_declaration":
            continue
        anns = _annotations(method, source)
        if "McpTool" in anns:
            ann, param_ann = anns["McpTool"], "McpToolParam"
        elif plain and "Tool" in anns and _spring_tool(anns["Tool"], source, imported):
            ann, param_ann = anns["Tool"], "ToolParam"
        else:
            continue
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = java_string(named, source) if named is not None else method_name
        if not endpoint:
            continue
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=SPRING_AI,
                handler=method_name,
                handler_line=line,
                description=java_string(_element(ann, "description", source), source),
                params=_params(method, source, param_ann),
                parent=owner.id if owner is not None else None,
            )
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = _annotated_tools(dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=SPRING_AI)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="java-springai",
        gate=Gate("java", order=30, guard=b"Tool", skip_fixtures=True),
        detect=_detect,
        frameworks=(SPRING_AI,),
    )
)
