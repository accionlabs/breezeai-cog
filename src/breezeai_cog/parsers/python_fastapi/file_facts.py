"""FastAPI wiring facts read from one Python file — shared by the FastAPI route detector (one
file) and the ``fastapi.wiring`` fact (:mod:`.wiring`, across files), so both read
``APIRouter`` / ``include_router`` / middleware the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ..python_common import calls, import_refs, keyword, literal, module_constants
from ..treesitter import node_text


#: A prefix that exists but has no resolvable value (``prefix=settings.API_PREFIX``): the
#: bindings behind it have an unknown address, so their endpoint is empty.
UNKNOWN = object()

DEPENDS = frozenset({"Depends", "Security", "fastapi.Depends", "fastapi.Security"})
ROUTERS = frozenset({"APIRouter", "fastapi.APIRouter", "FastAPI", "fastapi.FastAPI"})
#: The application constructors among :data:`ROUTERS`: an app is the root of its routes — never
#: passed to ``include_router`` — unless it is mounted as a sub-application (``.mount(…)``).
APPS = frozenset({"FastAPI", "fastapi.FastAPI"})

#: A prefix as recorded in the index (picklable): ``("lit", "/x")``, ``("const", file, name)``
#: for a module-level string constant (this file or another), or ``("unknown",)``.
PrefixSpec = tuple[str, ...]
#: The router an include site mounts: ``("local", var)``, ``("ext", file, var)``, or
#: ``("factory", file, function)`` — the router a factory function returns
#: (``include_router(make_router(), …)``).
RouterRef = tuple[str, ...]


def prefix_kwarg(call: Node, source: bytes) -> str | object:
    """The ``prefix=`` keyword of an ``APIRouter(...)`` / ``include_router(...)`` call: its literal
    value, ``""`` when absent, or :data:`UNKNOWN` when present but not a literal (Python has no
    compile-time constants, so a name or attribute has no value cog can trust here)."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        key = arg.child_by_field_name("name") if arg.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == "prefix":
            value = literal(arg.child_by_field_name("value"), source)
            return value if value is not None else UNKNOWN
    return ""


def dependencies(call: Node, source: bytes) -> list[str]:
    """Guard names from a ``dependencies=[Depends(fn), Security(fn, scopes=…)]`` keyword: the
    dependency callable of each entry (``fn`` / ``auth.require_user``). An entry that is not a
    ``Depends``/``Security`` call on a name is skipped (no fabricated name)."""
    value = keyword(call, source, "dependencies")
    if value is None:
        return []
    names: list[str] = []
    for el in value.named_children if value.type in ("list", "tuple") else []:
        fn = el.child_by_field_name("function") if el.type == "call" else None
        dep_args = el.child_by_field_name("arguments") if fn is not None else None
        first = next((a for a in dep_args.named_children if a.type != "keyword_argument"), None) \
            if dep_args is not None else None
        if (fn is not None and node_text(fn, source) in DEPENDS and first is not None
                and first.type in ("identifier", "attribute")):
            names.append(node_text(first, source))
    return names


def app_middleware(root: Node, source: bytes) -> dict[str, list[str]]:
    """App variable → its global middleware names, in source order: functions decorated
    ``@app.middleware("http")`` and classes passed to ``app.add_middleware(Cls, …)``. Middleware
    runs before every route of the app, whatever the registration order."""
    out: dict[str, list[str]] = {}

    def receiver_of(fn: Node | None, attr_name: str) -> str | None:
        if fn is None or fn.type != "attribute":
            return None
        attr, obj = fn.child_by_field_name("attribute"), fn.child_by_field_name("object")
        if attr is None or obj is None or node_text(attr, source) != attr_name:
            return None
        return node_text(obj, source)

    def walk(node: Node) -> None:
        for child in node.named_children:
            if child.type == "decorated_definition":
                fdef = next((c for c in child.named_children if c.type == "function_definition"), None)
                for dec in (c for c in child.named_children if c.type == "decorator"):
                    call = dec.named_children[0] if dec.named_children else None
                    app = receiver_of(call.child_by_field_name("function"), "middleware") \
                        if call is not None and call.type == "call" else None
                    if app is not None and fdef is not None:
                        name = fdef.child_by_field_name("name")
                        out.setdefault(app, []).append(node_text(name, source) if name else "")
            elif child.type == "call":
                app = receiver_of(child.child_by_field_name("function"), "add_middleware")
                args = child.child_by_field_name("arguments")
                first = next((a for a in args.named_children if a.type != "keyword_argument"), None) \
                    if args is not None else None
                if app is not None and first is not None and first.type in ("identifier", "attribute"):
                    out.setdefault(app, []).append(node_text(first, source))
            walk(child)

    walk(root)
    return out


# ── Repo-level facts (read by the Python index) ────────────────────────────────────────────

@dataclass
class FileFacts:
    """What one file contributes to the repo's FastAPI wiring. All plain data (picklable)."""

    #: Router / app variable → (own prefix, own dependency guards).
    routers: dict[str, tuple[PrefixSpec, tuple[str, ...]]] = field(default_factory=dict)
    #: Include sites: (receiver variable, mounted router, include prefix, include guards).
    includes: list[tuple[str, RouterRef, PrefixSpec, tuple[str, ...]]] = field(default_factory=list)
    #: App variable → global middleware names.
    middleware: dict[str, list[str]] = field(default_factory=dict)
    #: Module-level string constants of this file (``API_PREFIX = "/api/v1"``).
    constants: dict[str, str] = field(default_factory=dict)
    #: Router factories: top-level function → the router variable it creates and returns.
    factories: dict[str, str] = field(default_factory=dict)
    #: Router variables created inside a function: served only where the function's result
    #: is included, so unknown when no include is found.
    local_routers: set[str] = field(default_factory=set)
    #: App variables (``FastAPI(…)``) → the top-level function that creates them, ``""`` at
    #: module level.
    apps: dict[str, str] = field(default_factory=dict)
    #: Names a ``.mount(…)`` call of this file hands a sub-application by (:func:`mounted_names`).
    mounted: set[str] = field(default_factory=set)


def _enclosing_function(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type != "function_definition":
        parent = parent.parent
    return parent


def _outermost_function(fdef: Node) -> Node:
    while (outer := _enclosing_function(fdef)) is not None:
        fdef = outer
    return fdef


def _top_functions(root: Node) -> list[Node]:
    out = []
    for c in root.named_children:
        if c.type == "decorated_definition":
            c = c.child_by_field_name("definition") or c
        if c.type == "function_definition":
            out.append(c)
    return out


def _returned_names(fdef: Node, source: bytes) -> list[str]:
    """Identifiers returned by ``fdef``'s own ``return`` statements (nested functions skipped)."""
    out: list[str] = []

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "function_definition":
                continue
            if c.type == "return_statement":
                value = next(iter(c.named_children), None)
                if value is not None and value.type == "identifier":
                    out.append(node_text(value, source))
            walk(c)

    body = fdef.child_by_field_name("body")
    if body is not None:
        walk(body)
    return out


def file_facts(root: Node, source: bytes, repo_root: Path, rel: str) -> FileFacts:
    """The FastAPI wiring facts of one file (see :class:`FileFacts`)."""
    facts = FileFacts(constants=module_constants(root, source))
    imports = import_refs(root, source, repo_root, rel)

    def spec(call: Node) -> PrefixSpec:
        value = keyword(call, source, "prefix")
        if value is None:
            return ("lit", "")
        text = literal(value, source)
        if text is not None:
            return ("lit", text)
        if value.type == "identifier":
            name = node_text(value, source)
            if name in facts.constants:
                return ("const", rel, name)
            ref = imports.get(name)
            if ref is not None and ref[0] == "symbol":
                return ("const", ref[1], ref[2])
        if value.type == "attribute":
            obj, attr = value.child_by_field_name("object"), value.child_by_field_name("attribute")
            ref = imports.get(node_text(obj, source)) if obj is not None and obj.type == "identifier" else None
            if ref is not None and ref[0] == "module" and attr is not None:
                return ("const", ref[1], node_text(attr, source))
        return ("unknown",)

    for node in calls(root):
        fn = node.child_by_field_name("function")
        parent = node.parent
        if (fn is not None and node_text(fn, source) in ROUTERS
                and parent is not None and parent.type == "assignment"):
            target = parent.child_by_field_name("left")
            if target is not None and target.type == "identifier":
                name = node_text(target, source)
                facts.routers[name] = (spec(node), tuple(dependencies(node, source)))
                enclosing = _enclosing_function(node)
                if node_text(fn, source) in APPS:
                    outer = _outermost_function(enclosing) if enclosing is not None else None
                    fname = outer.child_by_field_name("name") if outer is not None else None
                    facts.apps[name] = node_text(fname, source) if fname is not None else ""
                elif enclosing is not None:
                    facts.local_routers.add(name)
    for fdef in _top_functions(root):
        fname = fdef.child_by_field_name("name")
        returned = _returned_names(fdef, source)
        routers = [r for r in returned if r in facts.local_routers]
        if fname is not None and len(set(routers)) == 1:
            facts.factories[node_text(fname, source)] = routers[0]
    for node in calls(root):
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        attr, obj = fn.child_by_field_name("attribute"), fn.child_by_field_name("object")
        args = node.child_by_field_name("arguments")
        first = next((a for a in args.named_children if a.type != "keyword_argument"), None) \
            if args is not None else None
        if attr is None or obj is None or first is None or node_text(attr, source) != "include_router":
            continue
        child: RouterRef | None = None
        if first.type == "identifier":
            name = node_text(first, source)
            ref = imports.get(name)
            if name in facts.routers:
                child = ("local", name)
            elif ref is not None and ref[0] == "symbol":
                child = ("ext", ref[1], ref[2])
        elif first.type == "attribute":
            mod, var = first.child_by_field_name("object"), first.child_by_field_name("attribute")
            ref = imports.get(node_text(mod, source)) if mod is not None and mod.type == "identifier" else None
            if ref is not None and ref[0] == "module" and var is not None:
                child = ("ext", ref[1], node_text(var, source))
        elif first.type == "call":  # include_router(make_router(), …): the factory's router
            callee = first.child_by_field_name("function")
            if callee is not None and callee.type == "identifier":
                cname = node_text(callee, source)
                cref = imports.get(cname)
                if cref is not None and cref[0] == "symbol":
                    child = ("factory", cref[1], cref[2])
                elif cref is None:
                    child = ("factory", rel, cname)
            elif callee is not None and callee.type == "attribute":
                mod, fn_name = callee.child_by_field_name("object"), callee.child_by_field_name("attribute")
                cref = imports.get(node_text(mod, source)) \
                    if mod is not None and mod.type == "identifier" else None
                if cref is not None and cref[0] == "module" and fn_name is not None:
                    child = ("factory", cref[1], node_text(fn_name, source))
        if child is not None:
            facts.includes.append(
                (node_text(obj, source), child, spec(node), tuple(dependencies(node, source))))
    facts.middleware = app_middleware(root, source) if b"middleware" in source else {}
    facts.mounted = mounted_names(root, source) if b".mount(" in source else set()
    return facts


def mounted_names(root: Node, source: bytes) -> set[str]:
    """The names a ``.mount(path, app)`` call hands a sub-application by: an identifier or
    attribute argument (``sub``, ``pkg.sub`` → ``sub``), the function a call argument calls
    (``create_sub()`` → ``create_sub``), and the function a module-level variable so passed was
    assigned from (``sub = create_sub()``). Matched by simple name across the repo, so a same-named
    app or factory elsewhere also counts as mounted — its address becomes unknown, never wrong."""
    out: set[str] = set()
    for node in calls(root):
        fn = node.child_by_field_name("function")
        attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
        args = node.child_by_field_name("arguments")
        if attr is None or node_text(attr, source) != "mount" or args is None:
            continue
        for arg in args.named_children:
            value = arg.child_by_field_name("value") if arg.type == "keyword_argument" else arg
            if value is not None and value.type == "call":
                value = value.child_by_field_name("function")
            if value is not None and value.type == "attribute":
                value = value.child_by_field_name("attribute")
            if value is not None and value.type == "identifier":
                out.add(node_text(value, source))
    for stmt in root.named_children:  # sub = create_sub(): the factory is mounted too
        node = stmt.named_children[0] if stmt.type == "expression_statement" and stmt.named_children else stmt
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if node.type != "assignment" or left is None or right is None or right.type != "call":
            continue
        callee = right.child_by_field_name("function")
        if callee is not None and callee.type == "attribute":
            callee = callee.child_by_field_name("attribute")
        if node_text(left, source) in out and callee is not None and callee.type == "identifier":
            out.add(node_text(callee, source))
    return out
