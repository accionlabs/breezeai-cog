"""Additive detector: calls of a TypeScript / JavaScript MCP **client**
(``@modelcontextprotocol/sdk/client/index.js``).

::

    import { Client } from "@modelcontextprotocol/sdk/client/index.js";
    const client = new Client({ name: "app", version: "1.0.0" });    # or a parameter typed Client
    await client.callTool({ name: "add", arguments: { a: 1 } })     → mcp_call  typescript-mcp  TOOL      add
    await client.readResource({ uri: "memo://insights" })           → mcp_call  typescript-mcp  RESOURCE  memo://insights
    await client.getPrompt({ name: "review", arguments: {} })       → mcp_call  typescript-mcp  PROMPT    review

* **Client receiver** — ``Client`` imported from the SDK's ``client/index.js`` (``import * as``
  too). A name whose nearest binding is a ``let`` / ``const`` / ``var`` assigned ``new Client(…)``
  or a parameter typed ``Client`` (a loop or ``catch`` variable, an untyped parameter, any other
  declaration hides it — a ``var`` of the function's in a nested block or loop head too; no
  binding in the file, or any other assignment to the name → not one).
  ``this.x`` in a class (not inside a ``function`` or an object literal's method, which rebind
  ``this``) whose one declaration of
  ``x`` — a field or a constructor parameter property — is typed ``Client`` or initialized
  ``new Client(…)``, or that only ever assigns ``this.x = new Client(…)``; two declarations →
  not one. The ``pg`` / ``redis`` ``Client``, and the AI SDK's ``experimental_createMCPClient``
  (not this class), are not MCP clients.
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
from .tools import (
    _FUNCTION_SCOPES,
    _PARAMETERS,
    FRAMEWORK,
    BlockDeclarations,
    _binds,
    block_declarations,
    bound_names,
)

_CLASSES = ("class_declaration", "class", "abstract_class_declaration")
#: Functions that give ``this`` a value of their own (an arrow function keeps the class's).
_THIS_SCOPES = (
    "function_declaration",
    "function_expression",
    "function",
    "generator_function_declaration",
    "generator_function",
)


def _text(node: Node | None, source: bytes) -> str:
    return node_text(node, source) if node is not None else ""


def _class_of(node: Node) -> Node | None:
    """The class whose ``this`` ``node`` sees; None outside a class, or inside a ``function`` or
    an object literal's method, which rebind ``this``."""
    cur = node.parent
    while cur is not None:
        if cur.type in _CLASSES:
            return cur
        if cur.type in _THIS_SCOPES:
            return None
        if (
            cur.type == "method_definition"
            and cur.parent is not None
            and cur.parent.type == "object"
        ):
            return None  # `{ run() { this… } }`: an object literal's method, `this` is the object
        cur = cur.parent
    return None


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

    blocks: BlockDeclarations = {}
    fields: dict[tuple[int, int], dict[str, bool]] = {}
    # Any `x = …` / `this.x = …` that is not `new Client(…)`: by text, the whole file.
    reassigned = {
        node_text(left, source)
        for node in _walk(root)
        if node.type == "assignment_expression"
        for left in [node.child_by_field_name("left")]
        if left is not None and not built(node.child_by_field_name("right"), source)
    }

    hoisted: dict[tuple[int, int], dict[str, list[Node | None]]] = {}

    def var_bindings(body: Node) -> dict[str, list[Node | None]]:
        """Each name a ``var`` binds anywhere in a function body (or the program) → its values:
        ``var`` reaches the whole function from a nested block or loop head. Nested functions
        keep their own."""
        key = (body.start_byte, body.end_byte)
        if key in hoisted:
            return hoisted[key]
        found: dict[str, list[Node | None]] = {}
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in _FUNCTION_SCOPES or node.type == "class_static_block":
                continue
            if node.type == "variable_declaration":
                for decl in node.named_children:
                    if decl.type == "variable_declarator":
                        value = decl.child_by_field_name("value")
                        for bound in bound_names(decl.child_by_field_name("name"), source):
                            found.setdefault(bound, []).append(value)
            elif (
                node.type == "for_in_statement"
                and _text(node.child_by_field_name("kind"), source) == "var"
            ):
                for bound in bound_names(node.child_by_field_name("left"), source):
                    found.setdefault(bound, []).append(None)  # `for (var client of …)`
            stack.extend(node.named_children)
        hoisted[key] = found
        return found

    def name_is_client(call: Node, name: str) -> bool:
        """The nearest binding of ``name`` at ``call`` is a client: a parameter typed ``Client``
        or a ``let`` / ``const`` / ``var`` assigned ``new Client(…)``. A loop or ``catch``
        variable, any other parameter or declaration, or no binding in the file → not one."""
        node = call.parent
        while node is not None:
            if node.type in _FUNCTION_SCOPES:
                single = node.child_by_field_name("parameter")  # `client => …`
                if single is not None and _binds(single, name, source):
                    return False
                params = node.child_by_field_name("parameters")
                for p in params.named_children if params is not None else ():
                    pattern = p.child_by_field_name("pattern") if p.type in _PARAMETERS else p
                    if _binds(pattern, name, source):
                        return p.type in _PARAMETERS and typed(p, source)
            elif node.type in ("statement_block", "program"):
                if node.type == "program" or (
                    node.parent is not None and node.parent.type in _FUNCTION_SCOPES
                ):
                    values = var_bindings(node).get(name)
                    if values is not None and not all(built(v, source) for v in values):
                        return False  # a `var` of the function's, in any nested block
                declared = block_declarations(node, source, blocks)
                if name in declared:
                    return built(declared[name], source)
            elif node.type in ("for_in_statement", "for_statement"):
                left = node.child_by_field_name("left") or node.child_by_field_name("initializer")
                if left is not None and name in _declared_by(left):
                    return False  # `for (const client of …)`
            elif node.type == "catch_clause":
                if _binds(node.child_by_field_name("parameter"), name, source):
                    return False
            node = node.parent
        return False

    def _declared_by(left: Node) -> list[str]:
        if left.type in ("lexical_declaration", "variable_declaration"):
            return [
                n
                for decl in left.named_children
                if decl.type == "variable_declarator"
                for n in bound_names(decl.child_by_field_name("name"), source)
            ]
        return bound_names(left, source)

    def class_fields(cls: Node) -> dict[str, bool]:
        """Each field of ``cls`` → whether it is a client: declared once in the class (a field,
        or a constructor parameter property) typed ``Client`` or initialized ``new
        Client(…)``, or never declared and only ever assigned ``this.x = new Client(…)``."""
        key = (cls.start_byte, cls.end_byte)
        if key in fields:
            return fields[key]
        decls: dict[str, list[bool]] = {}
        typed_decl: set[str] = set()
        assigned: dict[str, list[bool]] = {}
        body = cls.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            if member.type in ("public_field_definition", "field_definition"):
                name = member.child_by_field_name("name") or member.child_by_field_name("property")
                if name is None:
                    continue
                text = node_text(name, source)
                is_typed = typed(member, source)
                if is_typed:
                    typed_decl.add(text)
                decls.setdefault(text, []).append(
                    is_typed or built(member.child_by_field_name("value"), source)
                )
            elif (
                member.type == "method_definition"
                and _text(member.child_by_field_name("name"), source) == "constructor"
            ):
                params = member.child_by_field_name("parameters")
                for p in params.named_children if params is not None else ():
                    modifiers = {c.type for c in p.named_children}
                    if p.type not in _PARAMETERS or not modifiers & {
                        "accessibility_modifier",
                        "readonly",
                        "override_modifier",
                    }:
                        continue  # not a parameter property
                    pattern = p.child_by_field_name("pattern")
                    if pattern is None:
                        continue
                    text = node_text(pattern, source)
                    if typed(p, source):
                        typed_decl.add(text)
                    decls.setdefault(text, []).append(typed(p, source))
        for node in _walk(cls):
            if node.type != "assignment_expression":
                continue
            left = node.child_by_field_name("left")
            if (
                left is None
                or left.type != "member_expression"
                or _text(left.child_by_field_name("object"), source) != "this"
                or _class_of(node) != cls
            ):
                continue
            prop = left.child_by_field_name("property")
            if prop is not None:
                assigned.setdefault(node_text(prop, source), []).append(
                    built(node.child_by_field_name("right"), source)
                )
        out: dict[str, bool] = {}
        for field_name in set(decls) | set(assigned):
            found, writes = decls.get(field_name, []), assigned.get(field_name, [])
            if len(found) > 1:
                out[field_name] = False
            elif found:
                # A typed field holds a client whatever is assigned; an untyped one only while
                # every assignment is `new Client(…)`.
                out[field_name] = found[0] and (field_name in typed_decl or all(writes))
            else:
                out[field_name] = bool(writes) and all(writes)
        fields[key] = out
        return out

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
        if obj.type == "identifier":
            name = node_text(obj, source)
            if name in reassigned or not name_is_client(call, name):
                continue
        elif (
            obj.type == "member_expression"
            and _text(obj.child_by_field_name("object"), source) == "this"
        ):
            cls = _class_of(call)
            field = obj.child_by_field_name("property")
            if cls is None or field is None or not class_fields(cls).get(node_text(field, source)):
                continue
        else:
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
