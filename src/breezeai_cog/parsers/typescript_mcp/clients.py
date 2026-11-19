"""Additive detector: calls of a TypeScript / JavaScript MCP **client**
(``@modelcontextprotocol/sdk/client/index.js``).

::

    import { Client } from "@modelcontextprotocol/sdk/client/index.js";
    const client = new Client({ name: "app", version: "1.0.0" });    # or a parameter typed Client
    await client.callTool({ name: "add", arguments: { a: 1 } })     → mcp_call  typescript-mcp  TOOL      add
    await client.readResource({ uri: "memo://insights" })           → mcp_call  typescript-mcp  RESOURCE  memo://insights
    await client.getPrompt({ name: "review", arguments: {} })       → mcp_call  typescript-mcp  PROMPT    review

* **Client receiver** — a name (or ``this.x``) assigned ``new Client(…)``, or a parameter / field
  typed ``Client``, with ``Client`` imported from the SDK's ``client/index.js`` (``import * as``
  too); a nearer binding of the name hides it. The ``pg`` / ``redis`` ``Client``, and the AI SDK's
  ``experimental_createMCPClient`` (not this class), are not MCP clients.
* ``endpoint`` — ``name`` (``uri`` for ``readResource``) of the object literal passed first, a
  string literal; anything else → no record.
* The record is the call's own statement, with no handler. A client-only file gets no
  file-level framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import call_emit
from ..treesitter import node_text
from ..typescript.declarations import import_bindings
from .lowlevel import _pairs, _sdk_names, _string, _unparen, _walk
from .tools import FRAMEWORK, _shadowed

CLIENT_MODULES = frozenset(
    {
        "@modelcontextprotocol/sdk/client/index.js",
        "@modelcontextprotocol/sdk/client/index",
        "@modelcontextprotocol/sdk/client",
    }
)
#: Client methods → (method, key of the request object naming what they reach).
_CALLS = {
    "callTool": ("TOOL", "name"),
    "readResource": ("RESOURCE", "uri"),
    "getPrompt": ("PROMPT", "name"),
}


def _detect(dc: DetectContext) -> Detection | None:
    source, root = dc.source, dc.root
    if not any(m.encode() in source for m in _CALLS):
        return None
    classes = _sdk_names(import_bindings(root, source), CLIENT_MODULES, "Client")
    if not classes:
        return None

    def typed(param: Node, src: bytes) -> bool:
        ann = param.child_by_field_name("type")
        return ann is not None and node_text(ann, src).lstrip(": ").strip() in classes

    def built(value: Node | None, src: bytes) -> bool:
        value = _unparen(value)
        if value is None or value.type != "new_expression":
            return False
        ctor = value.child_by_field_name("constructor")
        return ctor is not None and node_text(ctor, src) in classes

    clients: set[str] = set()
    for node in _walk(root):
        if node.type == "new_expression" and built(node, source):
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                name = parent.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    clients.add(node_text(name, source))
            elif parent is not None and parent.type in (
                "public_field_definition",
                "field_definition",
            ):
                name = parent.child_by_field_name("name") or parent.child_by_field_name("property")
                if name is not None:
                    clients.add(f"this.{node_text(name, source)}")
        elif node.type in ("required_parameter", "optional_parameter", "public_field_definition"):
            name = node.child_by_field_name("pattern") or node.child_by_field_name("name")
            if name is not None and typed(node, source):
                text = node_text(name, source)
                clients.add(f"this.{text}" if node.type == "public_field_definition" else text)
    if not clients:
        return None
    reassigned = {
        node_text(left, source)
        for node in _walk(root)
        if node.type == "assignment_expression"
        for left in [node.child_by_field_name("left")]
        if left is not None and not built(node.child_by_field_name("right"), source)
    }
    emits: list[Emit] = []
    for call in _walk(root):
        if call.type != "call_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        kind = _CALLS.get(node_text(prop, source)) if prop is not None else None
        if obj is None or kind is None:
            continue
        receiver = node_text(obj, source)
        if receiver not in clients or receiver in reassigned:
            continue
        if obj.type == "identifier" and _shadowed(call, receiver, source, typed, built):
            continue
        args_node = call.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        request = _unparen(args[0]) if args else None
        if request is None or request.type != "object":
            continue
        value = _pairs(request, source).get(kind[1])
        endpoint = (
            _string(value, source)
            if value is not None and value.type in ("string", "template_string")
            else None
        )
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=FRAMEWORK)
            )
    return Detection(emits) if emits else None


register_detector(
    Detector(
        name="typescript-mcp-client",
        gate=Gate("typescript", order=95, guard=b"@modelcontextprotocol/sdk"),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
