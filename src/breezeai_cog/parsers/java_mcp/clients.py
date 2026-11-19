"""Additive detector: calls of an MCP **client** of the MCP Java SDK (``io.modelcontextprotocol``).

::

    McpSyncClient client = McpClient.sync(transport).build();   # or: var client = McpClient.sync(…)…build()
    client.callTool(new CallToolRequest("add", Map.of("a", 1)))  → mcp_call  java-mcp  TOOL      add
    client.readResource(new ReadResourceRequest("memo://x"))     → mcp_call  java-mcp  RESOURCE  memo://x
    client.getPrompt(new GetPromptRequest("review", Map.of()))   → mcp_call  java-mcp  PROMPT    review

* **Client receiver** — a name (or ``this.name``) every declaration of which in the file (a
  lambda's or a loop's variable of the same name counts as a declaration, never a client) has
  the type ``McpSyncClient`` / ``McpAsyncClient`` (a field, a local or a parameter), or is ``var``
  initialized by a ``McpClient.sync(…)`` / ``McpClient.async(…)`` chain. ``clients.get(0).callTool(…)`` (a
  Spring AI list of clients) is not one: which server it reaches is not in the code.
* **Request** — ``new CallToolRequest(name, arguments[, meta])`` (``(mapper, name, json)``
  when the first argument is not a string literal),
  ``CallToolRequest.builder(name)`` / ``.builder().name(name)…build()``, ``new
  ReadResourceRequest(uri)``, ``new GetPromptRequest(name, arguments)`` (or their builders), with a
  string literal; anything else → no record.
* The record is the call's own statement, with no handler; a client-only file gets no file-level
  framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import JAVA_MCP, java_string, walk
from ..mcp_common import call_emit
from ..treesitter import node_text
from .tools import _args, _builder_chain, _type_name

CLIENT_TYPES = frozenset({"McpSyncClient", "McpAsyncClient"})
#: Client methods → (method, request type, builder method naming what it reaches).
_CALLS = {
    "callTool": ("TOOL", "CallToolRequest", "name"),
    "readResource": ("RESOURCE", "ReadResourceRequest", "uri"),
    "getPrompt": ("PROMPT", "GetPromptRequest", "name"),
}


def _built_client(value: Node | None, source: bytes) -> bool:
    """``McpClient.sync(…)….build()`` / ``McpClient.async(…)….build()``."""
    cur = value
    calls: list[str] = []
    while cur is not None and cur.type == "method_invocation":
        name = cur.child_by_field_name("name")
        calls.append(node_text(name, source) if name is not None else "")
        obj = cur.child_by_field_name("object")
        if obj is not None and obj.type != "method_invocation":
            return (
                bool(calls)
                and calls[0] == "build"
                and calls[-1] in ("sync", "async")
                and node_text(obj, source).rsplit(".", 1)[-1] == "McpClient"
            )
        cur = obj
    return False


def _clients(root: Node, source: bytes) -> set[str]:
    """Names every declaration of which in the file proves an MCP client."""
    declared: dict[str, list[bool]] = {}
    for node in walk(root):
        if node.type in ("local_variable_declaration", "field_declaration"):
            type_node = node.child_by_field_name("type")
            type_text = node_text(type_node, source).split("<")[0] if type_node is not None else ""
            for decl in node.children_by_field_name("declarator"):
                name = decl.child_by_field_name("name")
                if name is None:
                    continue
                proven = type_text.rsplit(".", 1)[-1] in CLIENT_TYPES or (
                    type_text == "var" and _built_client(decl.child_by_field_name("value"), source)
                )
                declared.setdefault(node_text(name, source), []).append(proven)
        elif node.type in ("formal_parameter", "catch_formal_parameter", "enhanced_for_statement"):
            type_node = node.child_by_field_name("type")
            name = node.child_by_field_name("name")
            if name is not None:
                type_text = node_text(type_node, source).split("<")[0] if type_node else ""
                declared.setdefault(node_text(name, source), []).append(
                    node.type == "formal_parameter" and type_text.rsplit(".", 1)[-1] in CLIENT_TYPES
                )
        elif node.type == "lambda_expression":
            # `client -> …` / `(a, client) -> …`: an inferred parameter is never proven a client
            params = node.child_by_field_name("parameters")
            idents = (
                [params]
                if params is not None and params.type == "identifier"
                else [
                    c
                    for c in (params.named_children if params is not None else [])
                    if c.type == "identifier"
                ]
            )
            for ident in idents:
                declared.setdefault(node_text(ident, source), []).append(False)
    return {name for name, proofs in declared.items() if all(proofs)}


def _request_endpoint(node: Node, kind: tuple[str, str, str], source: bytes) -> str | None:
    if node.type == "object_creation_expression" and _type_name(node, source) == kind[1]:
        args = _args(node)
        first = java_string(args[0], source) if args else None
        if first is None and kind[1] == "CallToolRequest" and len(args) == 3:
            return java_string(args[1], source)  # (jsonMapper, name, jsonArguments)
        return first  # (name, arguments) / (name, arguments, meta) / (uri)
    chain = _builder_chain(node, source) if node.type == "method_invocation" else None
    if chain is None or chain[0] != kind[1] or chain[1][-1][0] != "build":
        return None
    calls = {method: args for method, args in chain[1]}
    named = calls.get(kind[2]) or (calls.get("builder") or [])[:1]
    return java_string(named[0], source) if len(named) == 1 else None


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"McpClient" not in source and b"McpSyncClient" not in source:
        return None  # neither a client type nor its factory
    if not any(m.encode() in source for m in _CALLS):
        return None
    clients = _clients(dc.root, source)
    if not clients:
        return None
    emits: list[Emit] = []
    for call in walk(dc.root):
        if call.type != "method_invocation":
            continue
        name = call.child_by_field_name("name")
        kind = _CALLS.get(node_text(name, source)) if name is not None else None
        obj = call.child_by_field_name("object")
        if kind is None or obj is None:
            continue
        receiver = node_text(obj, source)
        if obj.type == "field_access" and receiver.startswith("this."):
            receiver = receiver[len("this.") :]
        elif obj.type != "identifier":
            continue
        args = _args(call)
        if receiver not in clients or len(args) != 1:
            continue
        endpoint = _request_endpoint(args[0], kind, source)
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=JAVA_MCP)
            )
    return Detection(emits) if emits else None


register_detector(
    Detector(
        name="java-mcp-client",
        gate=Gate("java", order=45, guard=b"io.modelcontextprotocol"),
        detect=_detect,
        frameworks=(JAVA_MCP,),
    )
)
