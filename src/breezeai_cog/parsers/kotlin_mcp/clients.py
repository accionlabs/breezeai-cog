"""Additive detector: calls of an MCP **client** in Kotlin — the official Kotlin SDK's ``Client``
(``kotlin-mcp``) or the MCP Java SDK's ``McpSyncClient`` / ``McpAsyncClient`` (``java-mcp``).

::

    val client = Client(clientInfo = Implementation("app", "1.0"))   # io.modelcontextprotocol.kotlin.sdk.client.Client
    client.callTool(name = "add", arguments = mapOf("a" to 1))       → mcp_call  kotlin-mcp  TOOL      add
    client.readResource(ReadResourceRequest(ReadResourceRequestParams(uri = "memo://x")))
                                                                     → mcp_call  kotlin-mcp  RESOURCE  memo://x
    client.getPrompt(GetPromptRequest(GetPromptRequestParams(name = "review")))
                                                                     → mcp_call  kotlin-mcp  PROMPT    review
    val sync = McpClient.sync(transport).build()
    sync.callTool(McpSchema.CallToolRequest("add", mapOf()))         → mcp_call  java-mcp  TOOL  add

* **Client receiver** — a name (or ``this.name``) declared once in the file (a loop's or a
  lambda's variable of the same name counts as another declaration): typed ``Client`` (with
  the Kotlin SDK's ``Client`` imported) / ``McpSyncClient`` / ``McpAsyncClient``, or initialized by
  ``Client(…)`` (that import) or a ``McpClient.sync(…)`` / ``.async(…)`` chain.
* **Request** — the Kotlin SDK's ``callTool(name, …)`` (named or first), or a
  ``CallToolRequest`` / ``ReadResourceRequest`` / ``GetPromptRequest`` built with ``name =`` /
  ``uri =`` or its ``…Params(name = …)``; the Java SDK's request constructors as in Java. A
  string literal; anything else → no record.
* The record is the call's own statement, with no handler; a client-only file gets no file-level
  framework.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    JAVA_MCP,
    kotlin_aliases,
    kotlin_call_args,
    kotlin_string,
    kotlin_suffix_name,
    walk,
)
from ..mcp_common import call_emit
from ..treesitter import node_text
from .tools import KOTLIN_MCP, _builder_chain, _named, _positional, _type_callee

_KOTLIN_CLIENT_IMPORT = re.compile(
    rb"^[ \t]*import[ \t]+io\.modelcontextprotocol\.kotlin\.sdk\.client\.(?:Client\b(?![ \t]+as)|\*)",
    re.MULTILINE,
)
_JAVA_TYPES = frozenset({"McpSyncClient", "McpAsyncClient"})
#: Client methods → (method, request type, the field naming what they reach).
_CALLS = {
    "callTool": ("TOOL", "CallToolRequest", "name"),
    "readResource": ("RESOURCE", "ReadResourceRequest", "uri"),
    "getPrompt": ("PROMPT", "GetPromptRequest", "name"),
}


def _built_java_client(value: Node | None, source: bytes) -> bool:
    """``McpClient.sync(…)….build()`` / ``.async(…)``."""
    calls: list[str] = []
    cur = value
    while cur is not None and cur.type == "call_expression":
        callee = cur.named_children[0] if cur.named_children else None
        if callee is None or callee.type != "navigation_expression":
            return False
        calls.append(kotlin_suffix_name(callee, source))
        receiver = callee.named_children[0]
        if receiver.type != "call_expression":
            return (
                calls[0] == "build"
                and calls[-1] in ("sync", "async")
                and node_text(receiver, source).rsplit(".", 1)[-1] == "McpClient"
            )
        cur = receiver
    return False


def _clients(root: Node, source: bytes, kotlin_client: bool) -> dict[str, str]:
    """Names declared once in the file whose declaration proves a client → its framework."""
    declared: dict[str, list[str | None]] = {}

    def framework(type_node: Node | None, value: Node | None) -> str | None:
        type_name = node_text(type_node, source).split("<")[0].rstrip("?") if type_node else ""
        type_name = type_name.rsplit(".", 1)[-1]
        if type_name in _JAVA_TYPES or _built_java_client(value, source):
            return JAVA_MCP
        if kotlin_client and (
            type_name == "Client"
            or (
                value is not None
                and value.type == "call_expression"
                and value.named_children
                and node_text(value.named_children[0], source) == "Client"
            )
        ):
            return KOTLIN_MCP
        return None

    for node in walk(root):
        if node.type == "property_declaration":
            var = next((c for c in node.named_children if c.type == "variable_declaration"), None)
            if var is None:
                continue
            name = next((c for c in var.named_children if c.type == "simple_identifier"), None)
            type_node = next((c for c in var.named_children if c.type != "simple_identifier"), None)
            value = None
            after_eq = False
            for c in node.children:
                if c.type == "=":
                    after_eq = True
                elif after_eq and c.is_named:
                    value = c
                    break
            if name is not None:
                declared.setdefault(node_text(name, source), []).append(framework(type_node, value))
        elif node.type == "variable_declaration" and (
            node.parent is None or node.parent.type != "property_declaration"
        ):
            # `for (client in …)`, `{ client -> … }`, a destructuring: never proven a client
            name = next((c for c in node.named_children if c.type == "simple_identifier"), None)
            if name is not None:
                declared.setdefault(node_text(name, source), []).append(None)
        elif node.type in ("parameter", "class_parameter"):
            name = next((c for c in node.named_children if c.type == "simple_identifier"), None)
            type_node = next(
                (c for c in node.named_children if c.type in ("user_type", "nullable_type")), None
            )
            if name is not None:
                declared.setdefault(node_text(name, source), []).append(framework(type_node, None))
    return {
        name: found[0]
        for name, found in declared.items()
        if len(found) == 1 and found[0] is not None
    }


def _string_arg(args: list[tuple[str | None, Node]], key: str, source: bytes) -> str | None:
    pos = _positional(args)
    named = _named(args, key)
    return kotlin_string(named if named is not None else (pos[0] if pos else None), source)


def _request(
    node: Node | None, kind: tuple[str, str, str], source: bytes, aliases: dict[str, str]
) -> str | None:
    """``CallToolRequest(name = "x")`` / ``CallToolRequest(CallToolRequestParams(name = "x"))``
    (Kotlin SDK), ``McpSchema.CallToolRequest("x", args)`` / its builder (Java SDK)."""
    if node is None or node.type != "call_expression":
        return None
    chain = _builder_chain(node, source, aliases)
    if chain is not None:
        root, calls = chain
        if root != kind[1]:
            return None
        named = calls.get(kind[2]) or (calls.get("builder") or [])[:1]
        return kotlin_string(named[0], source) if len(named) == 1 else None
    if _type_callee(node, source, aliases) != kind[1]:
        return None
    args = kotlin_call_args(node, source)
    pos = _positional(args)
    first = _named(args, "params") or (pos[0] if pos else None)
    params_type = kind[1] + "Params"
    if (
        first is not None
        and first.type == "call_expression"
        and _type_callee(first, source, aliases) == params_type
    ):
        return _string_arg(kotlin_call_args(first, source), kind[2], source)
    if kind[1] == "CallToolRequest" and len(pos) == 3 and not any(k for k, _ in args):
        return kotlin_string(pos[1], source)  # Java SDK (jsonMapper, name, jsonArguments)
    return _string_arg(args, kind[2], source)


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if not any(m.encode() in source for m in _CALLS):
        return None
    kotlin_client = bool(_KOTLIN_CLIENT_IMPORT.search(source))
    clients = _clients(dc.root, source, kotlin_client)
    if not clients:
        return None
    aliases = kotlin_aliases(source)
    emits: list[Emit] = []
    for call in walk(dc.root):
        if call.type != "call_expression":
            continue
        callee = call.named_children[0] if call.named_children else None
        if callee is None or callee.type != "navigation_expression":
            continue
        kind = _CALLS.get(kotlin_suffix_name(callee, source))
        receiver = callee.named_children[0]
        if kind is None:
            continue
        if receiver.type == "simple_identifier":
            name = node_text(receiver, source)
        elif (
            receiver.type == "navigation_expression"
            and receiver.named_children
            and receiver.named_children[0].type == "this_expression"
        ):
            name = kotlin_suffix_name(receiver, source)
        else:
            continue
        framework = clients.get(name)
        if framework is None:
            continue
        args = kotlin_call_args(call, source)
        pos = _positional(args)
        if (
            framework == KOTLIN_MCP
            and kind[0] == "TOOL"
            and (_named(args, "name") is not None or (pos and pos[0].type == "string_literal"))
        ):
            endpoint = _string_arg(args, "name", source)  # callTool(name, arguments)
        else:
            endpoint = _request(pos[0] if len(pos) == 1 else None, kind, source, aliases)
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=framework)
            )
    return Detection(emits) if emits else None


register_detector(
    Detector(
        name="kotlin-mcp-client",
        gate=Gate("kotlin", order=45, guard=b"io.modelcontextprotocol"),
        detect=_detect,
        frameworks=(JAVA_MCP, KOTLIN_MCP),
    )
)
