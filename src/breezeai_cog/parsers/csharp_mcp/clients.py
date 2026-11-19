"""Additive detector: calls of a C# MCP **client** (``ModelContextProtocol.Client``).

::

    var client = await McpClient.CreateAsync(transport);       # or McpClientFactory.CreateAsync(…),
                                                               # or a field / parameter typed McpClient / IMcpClient
    await client.CallToolAsync("echo", args)                   → mcp_call  csharp-mcp  TOOL      echo
    await client.ReadResourceAsync("memo://insights")          → mcp_call  csharp-mcp  RESOURCE  memo://insights
    await client.GetPromptAsync("review", args)                → mcp_call  csharp-mcp  PROMPT    review

* **Client receiver** — a name every declaration of which in the file (a ``foreach`` or lambda
  variable of the same name counts as a declaration, never a client) is typed ``McpClient`` /
  ``IMcpClient``, or ``var`` initialized by ``[await] McpClient.CreateAsync(…)`` /
  ``McpClientFactory.CreateAsync(…)``; only in a file that uses the ``ModelContextProtocol``
  package.
* ``endpoint`` — the first argument (or ``toolName:`` / ``name:`` / ``uri:`` / ``uriTemplate:``),
  a string literal, or ``new Uri("…")`` of one for a resource; anything else → no record.
* The record is the call's own statement, with no handler; a client-only file gets no file-level
  framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import call_emit
from ..treesitter import node_text
from .tools import FRAMEWORK, _string, _walk

CLIENT_TYPES = frozenset({"McpClient", "IMcpClient"})
_FACTORIES = frozenset({"McpClient.CreateAsync", "McpClientFactory.CreateAsync"})
#: Client methods → (method, the names of the argument naming what they reach).
_CALLS = {
    "CallToolAsync": ("TOOL", ("toolName", "name")),
    "ReadResourceAsync": ("RESOURCE", ("uri", "uriTemplate")),
    "GetPromptAsync": ("PROMPT", ("name",)),
}


def _created(value: Node | None, source: bytes) -> bool:
    if value is not None and value.type == "await_expression" and value.named_children:
        value = value.named_children[0]
    if value is None or value.type != "invocation_expression":
        return False
    fn = value.child_by_field_name("function")
    text = node_text(fn, source) if fn is not None else ""
    return ".".join(text.split(".")[-2:]) in _FACTORIES


def _simple(type_node: Node | None, source: bytes) -> str:
    text = node_text(type_node, source) if type_node is not None else ""
    return text.rstrip("?").split("<")[0].rsplit(".", 1)[-1]


def _clients(root: Node, source: bytes) -> set[str]:
    declared: dict[str, list[bool]] = {}
    for node in _walk(root):
        if node.type == "variable_declaration":
            type_node = node.child_by_field_name("type")
            for decl in (c for c in node.named_children if c.type == "variable_declarator"):
                name = decl.child_by_field_name("name") or (
                    decl.named_children[0] if decl.named_children else None
                )
                if name is None:
                    continue
                value = next((c for c in decl.named_children if c != name), None)
                proven = _simple(type_node, source) in CLIENT_TYPES or (
                    type_node is not None
                    and type_node.type == "implicit_type"
                    and _created(value, source)
                )
                declared.setdefault(node_text(name, source), []).append(proven)
        elif node.type in ("foreach_statement", "implicit_parameter"):
            # `foreach (var client in …)`, `client => …`: never proven a client
            bound = node.child_by_field_name("left") if node.type == "foreach_statement" else node
            if bound is not None and bound.type in ("identifier", "implicit_parameter"):
                declared.setdefault(node_text(bound, source), []).append(False)
        elif node.type in ("parameter", "property_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                declared.setdefault(node_text(name, source), []).append(
                    _simple(node.child_by_field_name("type"), source) in CLIENT_TYPES
                )
    return {name for name, proofs in declared.items() if all(proofs)}


def _endpoint(call: Node, keys: tuple[str, ...], resource: bool, source: bytes) -> str | None:
    args_node = call.child_by_field_name("arguments")
    args = [a for a in (args_node.named_children if args_node else []) if a.type == "argument"]
    value: Node | None = None
    for a in args:
        name = a.child_by_field_name("name")
        if name is not None and node_text(name, source) in keys:
            value = a.named_children[-1]
    if value is None and args and args[0].child_by_field_name("name") is None:
        value = args[0].named_children[-1] if args[0].named_children else None
    if value is None:
        return None
    if resource and value.type == "object_creation_expression":
        if _simple(value.child_by_field_name("type"), source) != "Uri":
            return None
        inner = value.child_by_field_name("arguments")
        items = [a for a in (inner.named_children if inner else []) if a.type == "argument"]
        if len(items) != 1 or not items[0].named_children:
            return None
        value = items[0].named_children[-1]
    return _string(value, source)


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if not any(m.encode() in source for m in _CALLS):
        return None
    clients = _clients(dc.root, source)
    if not clients:
        return None
    emits: list[Emit] = []
    for call in _walk(dc.root):
        if call.type != "invocation_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_access_expression":
            continue
        obj, name = fn.child_by_field_name("expression"), fn.child_by_field_name("name")
        kind = _CALLS.get(node_text(name, source)) if name is not None else None
        if kind is None or obj is None or obj.type != "identifier":
            continue
        if node_text(obj, source) not in clients:
            continue
        endpoint = _endpoint(call, kind[1], kind[0] == "RESOURCE", source)
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=FRAMEWORK)
            )
    return Detection(emits) if emits else None


register_detector(
    Detector(
        name="csharp-mcp-client",
        gate=Gate("csharp", order=95, guard=b"ModelContextProtocol"),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
