"""Additive detector: tools of a C# MCP server (the ``ModelContextProtocol`` package).

::

    [McpServerToolType]
    public static class EchoTool
    {
        [McpServerTool(Name = "echo"), Description("Echoes the message")]   → mcp_tool  mcp-csharp  TOOL  echo
        public static string Echo([Description("The message")] string message, int times = 1,
                                  CancellationToken ct = default) => …

* ``[McpServerTool]`` is the SDK's own attribute, so it is an MCP tool wherever it appears.
* ``endpoint`` — ``Name =`` when a literal. Without it the SDK derives the name from the method,
  and how (casing, an ``Async`` suffix) depends on the SDK version: the endpoint is then left
  empty rather than guessed; ``handler`` still names the method.
* ``description`` — ``[Description("…")]`` on the method, else its XML doc ``<summary>``.
* ``params`` — ``[Description]`` per parameter; a parameter is optional only when it has a
  default (a nullable type alone stays required). Injected parameters (``CancellationToken``,
  ``IMcpServer`` / ``McpServer``, ``RequestContext<…>``, ``IProgress<…>``,
  ``IServiceProvider``, ``[FromServices]``) are not inputs.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "mcp-csharp"
_INJECTED = frozenset(
    {
        "CancellationToken",
        "IMcpServer",
        "McpServer",
        "RequestContext",
        "IProgress",
        "IServiceProvider",
        "IMcpEndpoint",
    }
)
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_MAX_DEFAULT = 200


def _string(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "attribute_argument":
        node = node.named_children[-1] if node.named_children else None
        if node is None:
            return None
    text = node_text(node, source)
    if node.type == "verbatim_string_literal":
        return text[2:-1].replace('""', '"')
    if node.type == "raw_string_literal":
        return text.strip('"').strip("\n")
    if node.type == "string_literal":
        return text[1:-1]
    return None


def _attributes(node: Node, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for lst in (c for c in node.named_children if c.type == "attribute_list"):
        for a in lst.named_children:
            if a.type == "attribute":
                name = a.child_by_field_name("name")
                if name is not None:
                    simple = node_text(name, source).rsplit(".", 1)[-1].removesuffix("Attribute")
                    out[simple] = a
    return out


def _args(attribute: Node) -> list[Node]:
    lst = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
    return (
        [a for a in lst.named_children if a.type == "attribute_argument"] if lst is not None else []
    )


def _named_arg(attribute: Node, key: str, source: bytes) -> Node | None:
    for a in _args(attribute):
        name = a.child_by_field_name("name")
        if name is not None and node_text(name, source) == key:
            return a
    return None


def _positional_string(attribute: Node | None, source: bytes) -> str | None:
    if attribute is None:
        return None
    for a in _args(attribute):
        if a.child_by_field_name("name") is None:
            return _string(a, source)
    return None


def _xml_summary(method: Node, source: bytes) -> str | None:
    lines: list[str] = []
    prev = method.prev_named_sibling
    while prev is not None and prev.type == "comment" and node_text(prev, source).startswith("///"):
        lines.insert(0, node_text(prev, source)[3:].strip())
        prev = prev.prev_named_sibling
    m = _SUMMARY.search(" ".join(lines))
    return " ".join(m.group(1).split()) if m else None


def _params(method: Node, source: bytes) -> list[ToolParam]:
    plist = method.child_by_field_name("parameters")
    out: list[ToolParam] = []
    for p in plist.named_children if plist is not None else []:
        if p.type != "parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        type_text = node_text(type_node, source)
        attrs = _attributes(p, source)
        if (
            type_text.rstrip("?").split("<")[0].rsplit(".", 1)[-1] in _INJECTED
            or "FromServices" in attrs
        ):
            continue
        after = [c for c in p.named_children if c.start_byte > name_node.end_byte]
        default = None
        if after:
            text = " ".join(node_text(after[-1], source).split()).lstrip("= ")
            default = text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"
        out.append(
            param(
                node_text(name_node, source),
                type_text,
                required=default is None,
                default=default,
                description=_positional_string(attrs.get("Description"), source),
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
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in _walk(dc.root):
        if method.type != "method_declaration":
            continue
        attrs = _attributes(method, source)
        tool = attrs.get("McpServerTool")
        name_node = method.child_by_field_name("name")
        if tool is None or name_node is None:
            continue
        method_name = node_text(name_node, source)
        named = _named_arg(tool, "Name", source)
        endpoint = _string(named, source) if named is not None else None
        if named is not None and not endpoint:
            continue  # a computed Name: the tool exists, but its name is unknown
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        description = _positional_string(attrs.get("Description"), source) or _xml_summary(
            method, source
        )
        emit = tool_emit(
            tool,
            source,
            endpoint=endpoint or "",
            framework=FRAMEWORK,
            handler=method_name,
            handler_line=line,
            description=description,
            params=_params(method, source),
            parent=owner.id if owner is not None else None,
            extra={"name": endpoint or method_name},
        )
        if not endpoint:
            emit.fields["endpoint"] = None
        found.append(emit)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="csharp-mcp",
        gate=Gate("csharp", order=90, guard=b"McpServerTool", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
