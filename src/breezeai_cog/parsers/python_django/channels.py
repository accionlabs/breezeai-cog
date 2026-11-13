"""Django Channels consumers: one ``route`` record per ``path()`` / ``re_path()`` that routes a
connection to a consumer (role table: ``docs/entry-point-roles/django.md``).

Channels routes a connection by protocol, then by URL, with Django's own pattern functions::

    # project/asgi.py
    application = ProtocolTypeRouter({
        "websocket": AuthMiddlewareStack(URLRouter(chat.routing.websocket_urlpatterns)),
    })
    # chat/routing.py
    websocket_urlpatterns = [re_path(r"ws/chat/(?P<room>\\w+)/$", consumers.ChatConsumer.as_asgi())]
    → WEBSOCKET /ws/chat/(?P<room>\\w+)/   handler ChatConsumer

The record sits on the pattern and leads to the consumer class. The consumer is the pattern's
view written ``Consumer.as_asgi()`` (Channels 3+), or a class the code shows to be a Channels
consumer passed directly (Channels 2). A websocket consumer (a base from
``channels.generic.websocket``) is a ``WEBSOCKET`` connection (``routeKind: ws``); an
``AsyncHttpConsumer`` answers ``ANY`` verb. Any other consumer takes the protocol of the
``ProtocolTypeRouter`` key it is routed under; when that is unknown, no record.

**Address.** Channels strips the leading ``/`` and matches the patterns the way Django does, a
nested ``URLRouter`` like an ``include()``: ``/`` + the patterns from the ``ProtocolTypeRouter`` down.
The patterns a list holds are known in its own file; where the list is routed from — a
``URLRouter`` in another file, under a prefix — is the ``django.channels`` fact. A list routed
from no ``ProtocolTypeRouter`` the code shows, or from two places, has no address.

It is an additive detector: a ``routing.py`` often imports neither Django nor Channels by a name a
parser claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector, register_fact,
)
from ..emits import Emit
from ..handler_sites import handler_location
from ..python_common import (
    Origins, argument, calls, field_text, literal, module_file, positional, top_statements,
)
from ..treesitter import node_text
from .patterns import Part, join
from .urls import FRAMEWORK, UrlFile
from .views import FileViews, linearize

FACT = "django.channels"

#: A list of patterns: ``("var", file, name)`` for a module-level variable, ``("inline", file,
#: byte offset)`` for a list written inside ``URLRouter(…)``.
ListKey = tuple[Any, ...]

_URL_MODULES = ("django.urls", "django.conf.urls")
_PATTERN_FUNCTIONS = {"path": False, "re_path": True, "url": True}
_URL_ROUTER = "channels.routing.URLRouter"
_PROTOCOL_ROUTER = "channels.routing.ProtocolTypeRouter"
#: Consumer bases → (method, routeKind) of the connection they accept.
_WEBSOCKET = ("WEBSOCKET", "ws")
_HTTP = ("ANY", "route")
_PROTOCOLS = {"websocket": _WEBSOCKET, "http": _HTTP}
#: Channels' consumer modules → the protocol their consumers accept.
_CONSUMER_MODULES = {"channels.generic.websocket": "websocket", "channels.generic.http": "http"}
#: Bytes a file with a Channels pattern or router contains.
_MARKERS = (b"as_asgi", b"URLRouter", b"Consumer")
_MAX_DEPTH = 10


@dataclass
class FileChannels:
    """What one file contributes. Plain data (picklable)."""

    #: (list routed by a ``ProtocolTypeRouter``, the protocol key — ``websocket`` / ``http``).
    roots: list[tuple[ListKey, str]] = field(default_factory=list)
    #: (list, the pattern above the nested one — None when not a literal, nested list).
    nested: list[tuple[ListKey, Part | None, ListKey]] = field(default_factory=list)


@dataclass(frozen=True)
class DjangoChannels:
    #: List → (the patterns above its own, the protocol it is routed under); None when unknown.
    served: dict[ListKey, tuple[tuple[Part, ...], str] | None] = field(default_factory=dict)


def django_channels(index: Any | None) -> DjangoChannels | None:
    found: DjangoChannels | None = index_fact(index, FACT)
    return found


class _File:
    def __init__(self, root: Node, source: bytes, rel: str, repo_root: Path | None) -> None:
        self.root, self.source, self.rel, self.repo_root = root, source, rel, repo_root
        self.origins = Origins(root, source)
        self.star = any(m in self.origins.star for m in _URL_MODULES)
        self.module_vars: dict[str, Node] = {}
        for stmt in top_statements(root):
            left, right = stmt.child_by_field_name("left"), stmt.child_by_field_name("right")
            if stmt.type == "assignment" and left is not None and left.type == "identifier" and right is not None:
                self.module_vars[node_text(left, source)] = right

    def is_(self, fn: Node | None, origin: str) -> bool:
        return fn is not None and self.origins.qualified(node_text(fn, self.source)) == origin

    def pattern_kind(self, fn: Node | None) -> str | None:
        """``path`` / ``re_path`` / ``url`` when ``fn`` is Django's, else None."""
        if fn is None or fn.type not in ("identifier", "attribute"):
            return None
        written = node_text(fn, self.source)
        name = written.rsplit(".", 1)[-1]
        if name not in _PATTERN_FUNCTIONS:
            return None
        origin = self.origins.qualified(written)
        if origin is not None:
            return name if origin.startswith(_URL_MODULES) else None
        return name if self.star and fn.type == "identifier" else None

    def lists(self, expr: Node | None, depth: int = 0) -> list[ListKey]:
        """The pattern lists an expression stands for: a literal list, a module-level variable
        (this file's or imported), and ``a + b``."""
        if expr is None or depth > _MAX_DEPTH:
            return []
        if expr.type in ("list", "tuple"):
            return [("inline", self.rel, expr.start_byte)]
        if expr.type == "binary_operator":
            return [*self.lists(expr.child_by_field_name("left"), depth + 1),
                    *self.lists(expr.child_by_field_name("right"), depth + 1)]
        if expr.type == "identifier" and node_text(expr, self.source) in self.module_vars:
            return [("var", self.rel, node_text(expr, self.source))]
        if expr.type in ("identifier", "attribute") and self.repo_root is not None:
            origin = self.origins.qualified(node_text(expr, self.source)) or ""
            level = len(origin) - len(origin.lstrip("."))
            module, _, var = origin.lstrip(".").rpartition(".")
            file = module_file(self.repo_root, self.rel, module, level) if var else None
            return [("var", file, var)] if file is not None else []
        return []

    def router_arg(self, expr: Node | None, depth: int = 0) -> Node | None:
        """The patterns of the ``URLRouter(…)`` an application expression is, through middleware
        wrappers (``AuthMiddlewareStack(URLRouter(…))``); None when it is not one."""
        if expr is None or expr.type != "call" or depth > _MAX_DEPTH:
            return None
        fn = expr.child_by_field_name("function")
        args = positional(expr)
        if self.is_(fn, _URL_ROUTER):
            return args[0] if args else None
        return self.router_arg(args[0], depth + 1) if args else None


def _list_of(f: _File, call: Node) -> ListKey | None:
    """The list a pattern call is an item of: a module-level variable's (also inside ``a + b``)
    or one written inside ``URLRouter(…)``."""
    lst = call.parent
    if lst is None or lst.type != "list":
        return None
    node = lst.parent
    while node is not None and node.type in ("binary_operator", "parenthesized_expression"):
        node = node.parent
    holder = node.parent if node is not None else None
    if holder is not None and holder.type == "expression_statement":
        holder = holder.parent
    if node is not None and node.type == "assignment" and holder is not None and holder.type == "module":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return ("var", f.rel, node_text(left, f.source))
    args = lst.parent
    if args is not None and args.type == "argument_list" and args.parent is not None \
            and f.is_(args.parent.child_by_field_name("function"), _URL_ROUTER):
        return ("inline", f.rel, lst.start_byte)
    return None


def _part(f: _File, call: Node) -> Part | None:
    kind = f.pattern_kind(call.child_by_field_name("function"))
    text = literal(argument(call, f.source, 0, "route"), f.source)
    return (text, _PATTERN_FUNCTIONS[kind]) if kind is not None and text is not None else None


def file_channels(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileChannels:
    """The routing of one file (see :class:`FileChannels`)."""
    f = _File(root, source, rel, repo_root)
    out = FileChannels()
    for call in calls(root):
        fn = call.child_by_field_name("function")
        if f.is_(fn, _PROTOCOL_ROUTER):
            mapping = positional(call)[0] if positional(call) else None
            for pair in mapping.named_children if mapping is not None and mapping.type == "dictionary" else []:
                protocol = literal(pair.child_by_field_name("key"), source) if pair.type == "pair" else None
                patterns = f.router_arg(pair.child_by_field_name("value")) if protocol in _PROTOCOLS else None
                out.roots.extend((key, protocol) for key in f.lists(patterns) if protocol is not None)
        elif f.pattern_kind(fn) is not None:  # path("chat/", URLRouter(…)): a nested router
            view = argument(call, source, 1, "view")
            inner = view.child_by_field_name("function") if view is not None and view.type == "call" else None
            parent = _list_of(f, call)
            if parent is not None and f.is_(inner, _URL_ROUTER) and view is not None:
                args = positional(view)
                out.nested.extend((parent, _part(f, call), key) for key in f.lists(args[0] if args else None))
    # A module-level list built from others (``patterns = chat + game``): each is nested at "".
    for name, right in f.module_vars.items():
        if right.type == "binary_operator":
            for key in f.lists(right):
                if key != ("var", rel, name) and key[0] == "var":
                    out.nested.append((("var", rel, name), ("", False), key))
    return out


def resolve_channels(files: dict[str, FileChannels]) -> DjangoChannels:
    sites: dict[ListKey, list[tuple[ListKey | None, Part | None, str | None]]] = {}
    for f in files.values():
        for key, protocol in f.roots:
            sites.setdefault(key, []).append((None, None, protocol))
        for parent, part, child in f.nested:
            sites.setdefault(child, []).append((parent, part, None))
    memo: dict[ListKey, tuple[tuple[Part, ...], str] | None] = {}

    def served(key: ListKey, seen: frozenset[ListKey]) -> tuple[tuple[Part, ...], str] | None:
        if key in memo:
            return memo[key]
        at = sites.get(key, [])
        result: tuple[tuple[Part, ...], str] | None = None
        if len(at) == 1 and key not in seen:
            parent, part, protocol = at[0]
            if parent is None and protocol is not None:
                result = ((), protocol)
            elif parent is not None and part is not None:
                above = served(parent, seen | {key})
                result = (above[0] + (part,), above[1]) if above is not None else None
        if not seen:
            memo[key] = result
        return result

    return DjangoChannels(served={key: served(key, frozenset()) for key in sorted(sites, key=repr)})


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, FileChannels] | None:
    facts = file_channels(root, source, rel, repo_root)
    return (rel, facts) if facts.roots or facts.nested else None


def _resolve(values: list[tuple[str, FileChannels]], index: Any) -> DjangoChannels | None:
    return resolve_channels(dict(values)) if values else None


register_fact(FactSpec(name=FACT, language="python", gate=(b"URLRouter",), collect=_collect, resolve=_resolve))


# ── detection ────────────────────────────────────────────────────────────────────────────


def _protocol(origin: str) -> str | None:
    """The protocol a Channels consumer base accepts (``websocket`` / ``http``), by origin."""
    for module, protocol in _CONSUMER_MODULES.items():
        if origin.startswith(module + "."):
            return protocol
    return None


def _consumer(u: UrlFile, f: _File, view: Node) -> tuple[str, str | None, tuple[str, str] | None] | None:
    """(name, the protocol its bases accept — None when they don't say, (file, name) of the class
    in the repo) of a pattern's consumer; None when the view is not a consumer: neither
    ``X.as_asgi()`` nor a class with a Channels base."""
    as_asgi = False
    if view.type == "call":
        fn = view.child_by_field_name("function")
        obj = fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None
        if obj is None or field_text(fn, "attribute", f.source) != "as_asgi":
            return None
        view, as_asgi = obj, True
    if view.type not in ("identifier", "attribute"):
        return None
    name = node_text(view, f.source).rsplit(".", 1)[-1]
    target = u.target(view)
    if target is not None and target[1] in u.views.get(target[0], FileViews()).classes:
        libs = [b[1] for b in linearize(u.views, target) if b[0] == "lib"]
        if not as_asgi and not any(o.startswith("channels.") for o in libs):
            return None
        return name, next((p for o in libs if (p := _protocol(o)) is not None), None), target
    if not as_asgi:  # a name the code does not show to be a consumer
        return None
    return name, _protocol(f.origins.qualified(node_text(view, f.source)) or ""), None


def detect_consumers(dc: DetectContext) -> list[Emit]:
    root, source, path = dc.root, dc.source, dc.path
    repo_root = Path(dc.ctx.repo_root) if dc.ctx.repo_root is not None else None
    f = _File(root, source, path, repo_root)
    fact = django_channels(dc.ctx.resolution_index)
    u: UrlFile | None = None
    out: list[Emit] = []
    for call in calls(root):
        if f.pattern_kind(call.child_by_field_name("function")) is None:
            continue
        view = argument(call, source, 1, "view")
        key = _list_of(f, call) if view is not None else None
        if view is None or key is None:
            continue
        u = u or UrlFile(root, source, path, dc.ctx.resolution_index, repo_root)
        consumer = _consumer(u, f, view)
        if consumer is None:
            continue
        name, protocol, target = consumer
        served = fact.served.get(key) if fact is not None else None
        kind = _PROTOCOLS.get(protocol or (served[1] if served is not None else ""))
        if kind is None:
            continue  # the protocol is not known: neither the consumer nor its router says
        part = _part(f, call)
        joined = join([*served[0], part]) if served is not None and part is not None else None
        site = None
        if target is not None:
            cls = u.views.get(target[0], FileViews()).classes.get(target[1])
            site = (target[0], cls.line) if cls is not None else None
        line, handler_path = handler_location(site, path)
        out.append(Emit(call, dict(
            semanticType="route", method=kind[0], endpoint=joined[0] if joined else None,
            isRegex=joined[1] if joined else None, framework=FRAMEWORK, handler=name,
            handlerLine=line, handlerPath=handler_path, routeKind=kind[1],
        )))
    return out


def _detect(dc: DetectContext) -> Detection | None:
    if not any(m in dc.source for m in _MARKERS):
        return None
    found = detect_consumers(dc)
    return Detection(found, label=FRAMEWORK) if found else None


register_detector(Detector(
    name="django-channels",
    gate=Gate("python", order=40, skip_fixtures=True),  # guards itself: several markers
    detect=_detect,
    frameworks=(FRAMEWORK,),
))
