"""Plain Java syntax-tree reads shared by the Vert.x detectors (``events.py``, ``legacy.py``):
method invocations, string literals, a local's assigned value, the class of a ``new X(…)``
handler, and the file's imports and package — plus the two reads every Vert.x detector shares:
an address argument folded to a constant, and where a handler class's ``handle`` is."""

from __future__ import annotations

from tree_sitter import Node

from ..addresses import informative, mark_runtime, runtime_name, source_kind
from ..handler_sites import handler_location, locate_method
from ..treesitter import node_text
from ..vertx_common import render_address


def invocations(root: Node) -> list[Node]:
    """Every ``method_invocation`` under ``root``, in source order."""
    return calls_and_creations(root)[0]


def calls_and_creations(root: Node) -> tuple[list[Node], list[Node]]:
    """Every ``method_invocation``, and every ``object_creation_expression`` and
    ``explicit_constructor_invocation`` (``super(…)`` / ``this(…)``) under ``root``, each in
    source order, from one walk."""
    calls: list[Node] = []
    creations: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            calls.append(n)
        elif n.type in ("object_creation_expression", "explicit_constructor_invocation"):
            creations.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return calls, creations


def java_string(node: Node, source: bytes) -> str | None:
    """A Java ``string_literal`` node → its text value (Java has no string interpolation)."""
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')


def local_value(at: Node, name: str, source: bytes) -> Node | None:
    """The value given to ``name`` in the method enclosing ``at`` — a local initializer or an
    assignment (``impl = new Impl()``, also to a field) — when exactly one is found."""
    method: Node | None = at
    while method is not None and method.type not in ("method_declaration",
                                                     "constructor_declaration"):
        method = method.parent
    found: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            nm, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and value is not None and node_text(nm, source) == name:
                found.append(value)
        elif n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and \
                    node_text(left, source).removeprefix("this.") == name:
                found.append(right)
        for c in n.named_children:
            walk(c)

    if method is not None:
        walk(method)
    return found[0] if len(found) == 1 else None


def handler_class(arg: Node | None, source: bytes) -> str | None:
    """The class of a handler given as ``new LoadHandler(…)`` (no anonymous body) — passed
    directly or through a local assigned once in the enclosing method — else None."""
    if arg is not None and arg.type == "identifier":
        arg = local_value(arg, node_text(arg, source), source)
    if arg is None or arg.type != "object_creation_expression" \
            or any(c.type == "class_body" for c in arg.named_children):
        return None
    typ = arg.child_by_field_name("type")
    return node_text(typ, source).split("<", 1)[0] if typ is not None else None


def anonymous_handle(arg: Node | None, source: bytes) -> int | None:
    """The declaration line of ``handle`` in a handler given as an anonymous class
    (``new Handler<Message>() { public void handle(Message m) { … } }``): every Vert.x handler
    type (``Handler``, ``AsyncResultHandler``, ``VoidHandler``) has one method, ``handle``, so
    that method is the code that runs. It is the start line of the anonymous class's ``handle``
    Function (annotations included). None when ``arg`` is no anonymous class, or its body
    declares no ``handle`` or more than one."""
    if arg is None or arg.type != "object_creation_expression":
        return None
    body = next((c for c in arg.named_children if c.type == "class_body"), None)
    if body is None:
        return None
    handles = [m for m in body.named_children if m.type == "method_declaration"
               and (nm := m.child_by_field_name("name")) is not None
               and node_text(nm, source) == "handle"]
    return handles[0].start_point[0] + 1 if len(handles) == 1 else None


def imports_and_package(root: Node, source: bytes) -> tuple[list[str], list[str], str]:
    """This file's single-type imports, on-demand (``.*``) import packages and its package."""
    singles: list[str] = []
    wildcards: list[str] = []
    package = ""
    for node in root.named_children:
        if node.type == "import_declaration":
            text = node_text(node, source).removeprefix("import").strip().rstrip(";").strip()
            if text.startswith("static "):
                continue
            if text.endswith(".*"):
                wildcards.append(text[:-2].strip())
            else:
                singles.append(text)
        elif node.type == "package_declaration":
            nm = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
            package = node_text(nm, source) if nm is not None else ""
    return singles, wildcards, package


def name_of(node: Node | None, source: bytes) -> str:
    """A node's text; empty for no node."""
    return node_text(node, source) if node is not None else ""


def plain(text: str) -> str:
    """A name without its ``this.``."""
    return text.removeprefix("this.").strip()


def arguments(call: Node) -> list[Node]:
    """A call's or creation's argument nodes."""
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def known_address(node: Node, source: bytes, consts: dict[str, str]) -> str | None:
    """An argument folded to a constant, else None (never a ``{name}``): for a verb."""
    value = render_address(node, source, consts, java_string)
    return value if value is not None and "{" not in value else None


def value_source(node: Node, source: bytes) -> Node:
    """What names a value: a local variable assigned once from a configuration or environment
    lookup is named after that lookup (``String address = config().getString("a"); …
    send(address)`` → ``config:a``); any other node names itself."""
    if node.type == "identifier":
        init = local_value(node, node_text(node, source), source)
        if init is not None and init.type == "method_invocation" \
                and source_kind(node_text(init, source)) is not None:
            return init
    return node


def event_address(node: Node, source: bytes, consts: dict[str, str]) -> str | None:
    """An event-bus address: its value when it folds to a constant, else every part read at
    runtime marked ``${…}`` (``addresses.py``: ``${config:api-verticle.address}``,
    ``${prefix}.orders``); None when it says nothing."""
    value = render_address(node, source, consts, java_string)
    if value is None:
        value = "{" + runtime_name(node_text(value_source(node, source), source)) + "}"
    return informative(mark_runtime(value))


def request_address(node: Node, source: bytes, consts: dict[str, str]) -> str | None:
    """An outbound URI argument: its value when it folds, else a ``{name}`` placeholder per part
    read at runtime (the executor marks a base read at runtime ``${…}``, ``addresses.py``)."""
    value = render_address(node, source, consts, java_string)
    if value is None:
        value = "{" + runtime_name(node_text(value_source(node, source), source)) + "}"
    return value


def handle_location(index: object | None, impl: str | None, path: str
                    ) -> tuple[int | None, str | None]:
    """(handlerLine, handlerPath) of class ``impl``'s ``handle`` method, when the repo has it."""
    found = locate_method(index, "java", impl, "handle") if impl else None
    return handler_location(found, path)
