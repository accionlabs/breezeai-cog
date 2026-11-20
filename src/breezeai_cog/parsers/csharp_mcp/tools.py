"""Additive detector: tools of a C# MCP server (the ``ModelContextProtocol`` package).

::

    [McpServerToolType]
    public static class EchoTool
    {
        [McpServerTool(Name = "echo"), Description("Echoes the message")]   → mcp_tool  csharp-mcp  TOOL  echo
        public static string Echo([Description("The message")] string message, int times = 1,
                                  CancellationToken ct = default) => …

* ``[McpServerTool]`` is the SDK's own attribute, so it is an MCP tool wherever it appears.
* ``endpoint`` — ``Name =`` when a literal. Without it, the name the SDK derives from the method
  (``McpServerTool.Create`` → ``DeriveName``, SDK 0.3 and later): a trailing ``Async`` dropped when
  the method returns ``Task`` / ``ValueTask`` / ``IAsyncEnumerable``, anything but ASCII letters
  and digits → ``_``, then snake_case (``JsonNamingPolicy.SnakeCaseLower``): ``GetWeatherAsync``
  → ``get_weather``. Earlier previews kept the method name; current releases are assumed.
* ``description`` — ``[Description("…")]`` on the method, else its XML doc ``<summary>``.
* ``params`` — ``[Description]`` per parameter; a parameter is optional only when it has a
  default (a nullable type alone stays required). Injected parameters (``CancellationToken``,
  ``IMcpServer`` / ``McpServer``, ``RequestContext<…>``, ``IProgress<…>``,
  ``IServiceProvider``, ``[FromServices]``) are not inputs.

Resources and prompts, the same way::

    [McpServerResource(UriTemplate = "users://{id}", Name = "User"), Description("A user")]
    public static string User(string id) => …   → mcp_resource  csharp-mcp  RESOURCE  users://{id}   params id
    [McpServerPrompt(Name = "review"), Description("Review code")]
    public static string Review([Description("The code")] string code) => …
                                                → mcp_prompt  csharp-mcp  PROMPT  review   params code

* ``mcp_resource`` — ``UriTemplate =`` a literal; without it the SDK derives the URI from the name
  and the parameters, which is not read → no record. ``params`` are the template's simple
  ``{variables}`` (``[]`` for a fixed URI; None with an RFC 6570 operator). ``name`` is
  ``Name =`` when a literal, else the URI.
* ``mcp_prompt`` — ``Name =`` when a literal, else the name derived as for a tool; ``params`` are
  the method's parameters, read as for a tool (the SDK builds the prompt's arguments from them)
  but with name, required and description only: a prompt argument has no type or default.
* ``description`` — as for a tool.
"""

from __future__ import annotations

import re
from typing import Any

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    TOOL,
    guarded_one,
    mcp_emit,
    param,
    prompt_params,
    uri_template_params,
)
from ..treesitter import node_text

FRAMEWORK = "csharp-mcp"
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


def _params(method: Node, source: bytes) -> list[Parameter]:
    plist = method.child_by_field_name("parameters")
    out: list[Parameter] = []
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


_ASYNC_RETURNS = frozenset({"Task", "ValueTask", "IAsyncEnumerable"})
_NOT_ASCII_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def _snake_case_lower(name: str) -> str:
    """``JsonNamingPolicy.SnakeCaseLower``: ``GetHTTPStatus2`` → ``get_http_status2``."""
    out: list[str] = []
    state = ""  # "", "upper", "lower" (a lowercase letter or a digit)
    for i, ch in enumerate(name):
        if ch.isupper():
            if state == "lower" or (
                state == "upper" and i + 1 < len(name) and name[i + 1].islower()
            ):
                out.append("_")
            out.append(ch.lower())
            state = "upper"
        elif ch.islower() or ch.isdigit():
            out.append(ch)
            state = "lower"
        else:
            out.append(ch)
            state = ""
    return "".join(out)


def _derived_name(method: Node, method_name: str, source: bytes) -> str:
    """The tool name the SDK derives from a method without ``Name =`` (see the module doc)."""
    name = method_name
    returns = method.child_by_field_name("returns") or method.child_by_field_name("type")
    simple = node_text(returns, source).split("<")[0].rsplit(".", 1)[-1] if returns else ""
    if simple in _ASYNC_RETURNS and name.endswith("Async") and len(name) > len("Async"):
        name = name[: -len("Async")]
    name = _NOT_ASCII_ALNUM.sub("_", name).strip("_") or method_name
    return _snake_case_lower(name)


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


#: The SDK's attributes → the kind of record they declare.
_ATTRIBUTES = (
    ("McpServerTool", TOOL),
    ("McpServerResource", RESOURCE),
    ("McpServerPrompt", PROMPT),
)


def _entry(
    method: Node,
    method_name: str,
    attr: Node,
    attrs: dict[str, Node],
    kind: tuple[str, str],
    source: bytes,
    functions: dict[tuple[str, int], Any],
) -> Emit | None:
    """The record ``attr`` (``[McpServerTool]`` / ``Resource`` / ``Prompt``) declares on
    ``method``; None for a computed name or URI."""
    params: list[Parameter] | None
    declared: str | None = None  # a resource's own literal ``Name =``
    if kind == RESOURCE:
        endpoint = _string(_named_arg(attr, "UriTemplate", source), source)
        declared = _string(_named_arg(attr, "Name", source), source)
        params = uri_template_params(endpoint) if endpoint else None
    else:
        named = _named_arg(attr, "Name", source)
        endpoint = (
            _string(named, source)
            if named is not None
            else _derived_name(method, method_name, source)
        )
        params = _params(method, source)
        if kind == PROMPT:
            params = prompt_params(params)  # a prompt argument has no type or default
    if not endpoint:
        return None  # a computed name: the record exists, but its name is unknown
    line = method.start_point[0] + 1
    owner = functions.get((method_name, line))
    description = _positional_string(attrs.get("Description"), source) or _xml_summary(
        method, source
    )
    return mcp_emit(
        attr,
        source,
        kind=kind,
        endpoint=endpoint,
        framework=FRAMEWORK,
        handler=method_name,
        handler_line=line,
        description=description,
        params=params,
        parent=owner.id if owner is not None else None,
        name=declared,
    )


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found: list[Emit] = []
    for method in _walk(dc.root):
        if method.type != "method_declaration":
            continue
        attrs = _attributes(method, source)
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        for attr_name, kind in _ATTRIBUTES:
            attr = attrs.get(attr_name)
            if attr is None:
                continue
            args = (method, method_name, attr, attrs, kind, source, functions)
            # A failing resource / prompt reader never drops the file's tools.
            emit = _entry(*args) if kind == TOOL else guarded_one(dc.path, _entry, *args)
            if emit is not None:
                found.append(emit)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="csharp-mcp",
        gate=Gate("csharp", order=90, guard=b"McpServer", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
