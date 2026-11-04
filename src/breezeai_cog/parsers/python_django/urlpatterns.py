"""The URL patterns of one urlconf module — the patterns Django serves from it.

A urlconf's patterns are its module-level ``urlpatterns``: assigned, added to (``+=``,
``.append``, ``.extend``, ``.insert``, also under a module-level ``if settings.DEBUG:``), built
from lists, ``+`` and helpers (``i18n_patterns(…)``, DRF's ``format_suffix_patterns(…)``), and from
other module-level lists of the same file. Each ``path()`` / ``re_path()`` / ``url()`` is either a
**binding** (it names a view) or an **include** (it names another urlconf, an inline list, or a
library's URLs); an inline include is followed here, with its prefix carried down.

Only Django's own ``path`` / ``re_path`` / ``url`` / ``include`` count (by import), so a helper of
the same name is not taken for a URL pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from ..python_common import Origins, argument, field_text, literal, module_file, positional
from ..treesitter import node_text
from .patterns import Part

_URL_MODULES = ("django.urls", "django.conf.urls")
_PATTERN_FUNCTIONS = {"path": False, "re_path": True, "url": True}
_LIST_HELPERS = frozenset({"format_suffix_patterns"})
_MAX_DEPTH = 20


@dataclass
class Binding:
    """A pattern that names a view."""

    call: Node
    #: Patterns above it in this module (inline includes); None when one is unknown.
    prefix: tuple[Part, ...] | None
    #: Its own pattern; None when it is not a literal.
    route: Part | None
    view: Node


@dataclass
class Include:
    """A pattern that names other patterns: a urlconf module, a router, or a library's URLs."""

    call: Node
    prefix: tuple[Part, ...] | None  # including the include's own pattern
    #: What it includes, as written: ``("module", dotted)`` for a string, ``("name", node)`` for a
    #: name or attribute (a module, a router's ``.urls``, ``admin.site.urls``).
    target: tuple[str, object]


class _Walk:
    def __init__(self, root: Node, source: bytes, origins: Origins) -> None:
        self.source, self.origins = source, origins
        self.star = any(m in origins.star for m in _URL_MODULES)
        #: Module-level names → the expression assigned; None when assigned more than once.
        self.lists: dict[str, Node | None] = {}
        for stmt in _module_statements(root):
            left = stmt.child_by_field_name("left") if stmt.type == "assignment" else None
            right = stmt.child_by_field_name("right") if stmt.type == "assignment" else None
            if left is not None and left.type == "identifier" and right is not None \
                    and node_text(left, source) != "urlpatterns":
                name = node_text(left, source)
                self.lists[name] = right if name not in self.lists else None
        self.bindings: list[Binding] = []
        self.includes: list[Include] = []

    def django(self, fn: Node | None, names: frozenset[str] | dict[str, bool]) -> str | None:
        """The Django URL function ``fn`` names (``path``, ``include`` …), or None."""
        if fn is None or fn.type not in ("identifier", "attribute"):
            return None
        written = node_text(fn, self.source)
        name = written.rsplit(".", 1)[-1]
        if name not in names:
            return None
        origin = self.origins.qualified(written)
        if origin is not None:
            return name if origin.startswith(_URL_MODULES) else None
        return name if self.star and fn.type == "identifier" else None

    def patterns(self, expr: Node | None, prefix: tuple[Part, ...] | None, depth: int) -> None:
        """Every pattern of the list expression ``expr``."""
        if expr is None or depth > _MAX_DEPTH:
            return
        if expr.type in ("list", "tuple", "parenthesized_expression"):
            for el in expr.named_children:
                self.element(el, prefix, depth)
        elif expr.type == "binary_operator":
            self.patterns(expr.child_by_field_name("left"), prefix, depth + 1)
            self.patterns(expr.child_by_field_name("right"), prefix, depth + 1)
        elif expr.type == "identifier":
            target = self.lists.get(node_text(expr, self.source))
            if target is not None:
                self.patterns(target, prefix, depth + 1)
        elif expr.type == "call":
            fn = expr.child_by_field_name("function")
            name = field_text(expr, "function", self.source).rsplit(".", 1)[-1]
            if name == "i18n_patterns":  # the language prefix is chosen at run time
                for arg in positional(expr):
                    self.element(arg, None, depth)
            elif name in _LIST_HELPERS and positional(expr):
                self.patterns(positional(expr)[0], prefix, depth + 1)
            elif self.django(fn, _PATTERN_FUNCTIONS) is not None:
                self.element(expr, prefix, depth)
        elif expr.type == "attribute":  # urlpatterns = router.urls
            self.includes.append(Include(expr, prefix, ("name", expr)))

    def element(self, el: Node, prefix: tuple[Part, ...] | None, depth: int) -> None:
        if el.type == "list_splat":  # *router.urls
            inner = el.named_children[0] if el.named_children else None
            if inner is not None:
                self.patterns(inner, prefix, depth + 1)
            return
        if el.type != "call":
            return
        kind = self.django(el.child_by_field_name("function"), _PATTERN_FUNCTIONS)
        if kind is None:
            return
        route_node = argument(el, self.source, 0, "route")
        text = literal(route_node, self.source)
        route: Part | None = (text, _PATTERN_FUNCTIONS[kind]) if text is not None else None
        view = argument(el, self.source, 1, "view")
        if view is None:
            return
        here = prefix + (route,) if prefix is not None and route is not None else None
        included = self.included(view)
        if included is None:
            self.bindings.append(Binding(el, prefix, route, view))
        elif included.type in ("list", "tuple") and not _is_module_tuple(included, self.source):
            self.patterns(included, here, depth + 1)
        elif included.type == "identifier" and node_text(included, self.source) in self.lists:
            self.patterns(included, here, depth + 1)
        elif included.type == "string":
            dotted = literal(included, self.source)
            if dotted is not None:
                self.includes.append(Include(el, here, ("module", dotted)))
        elif included.type in ("identifier", "attribute"):
            self.includes.append(Include(el, here, ("name", included)))

    def included(self, view: Node) -> Node | None:
        """What a pattern's view argument includes — the argument of ``include(…)`` (the first
        item of a ``(patterns, app_name)`` tuple), or a list / ``X.urls`` given directly; None
        when it names a view."""
        if view.type == "call" and self.django(view.child_by_field_name("function"), frozenset({"include"})):
            args = positional(view)
            arg = args[0] if args else None
            if arg is not None and arg.type in ("tuple", "parenthesized_expression") \
                    and _is_module_tuple(arg, self.source):
                arg = arg.named_children[0]
            return arg
        if view.type in ("list", "tuple"):
            return view
        if view.type == "attribute" and field_text(view, "attribute", self.source) == "urls":
            return view
        return None



def _is_module_tuple(node: Node, source: bytes) -> bool:
    """``(patterns, "app_name")`` — a 2- or 3-tuple whose second item is a string."""
    items = node.named_children
    return node.type in ("tuple", "parenthesized_expression") and len(items) in (2, 3) \
        and items[1].type == "string"


def _module_statements(root: Node) -> list[Node]:
    """Module-level statements, also inside module-level ``if`` / ``else`` blocks, with an
    ``expression_statement`` unwrapped."""
    out: list[Node] = []

    def visit(block: Node) -> None:
        for stmt in block.named_children:
            if stmt.type == "if_statement":
                for part in stmt.named_children:
                    if part.type == "block":
                        visit(part)
                    elif part.type in ("else_clause", "elif_clause"):
                        body = part.child_by_field_name("body") or next(
                            (c for c in part.named_children if c.type == "block"), None)
                        if body is not None:
                            visit(body)
            elif stmt.type == "expression_statement" and stmt.named_children:
                out.append(stmt.named_children[0])
            else:
                out.append(stmt)

    visit(root)
    return out


def urlconf(root: Node, source: bytes, origins: Origins) -> tuple[list[Binding], list[Include]] | None:
    """The bindings and includes of a module's ``urlpatterns``; None when it has none."""
    walk = _Walk(root, source, origins)
    found = False
    for stmt in _module_statements(root):
        if stmt.type in ("assignment", "augmented_assignment"):
            left = stmt.child_by_field_name("left")
            if left is not None and node_text(left, source) == "urlpatterns":
                found = True
                walk.patterns(stmt.child_by_field_name("right"), (), 0)
        elif stmt.type == "call":
            fn = stmt.child_by_field_name("function")
            obj = fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None
            method = field_text(fn, "attribute", source) if fn is not None and fn.type == "attribute" else ""
            if obj is None or node_text(obj, source) != "urlpatterns":
                continue
            args = positional(stmt)
            if method in ("append", "insert") and args:
                found = True
                walk.element(args[-1], (), 0)
            elif method == "extend" and args:
                found = True
                walk.patterns(args[0], (), 0)
    return (walk.bindings, walk.includes) if found else None


def module_target(dotted: str, rel: str, repo_root: Path | None) -> str | None:
    """The repo file of an ``include("shop.urls")`` module, found the way Django imports it —
    from the project's root on ``sys.path``, i.e. a folder above the including file."""
    return module_file(repo_root, rel, dotted, 0) if repo_root is not None else None
