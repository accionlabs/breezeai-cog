"""The names in a Python file that are HTTP-client sessions, for the shared api-call classifier.

``session`` is no HTTP-client name on its own (``detection/api_calls.py``): a SQLAlchemy
``db.session.delete(order)`` deletes a row. A receiver is an HTTP session only where the file
shows it::

    import requests, httpx, aiohttp
    session = requests.Session()                 # session.delete(url)      → api_call DELETE
    self.http = httpx.Client()                   # self.http.get(url)       → api_call GET
    with httpx.Client() as client: ...           # client.delete(url)       → api_call DELETE
    async with aiohttp.ClientSession() as s: ... # s.get(url)               → api_call GET
    def fetch(session: requests.Session): ...    # session.get(url)         → api_call GET
    def _session(self) -> httpx.Client: ...      # self._session().get(url) → api_call GET
    s = await registry.get_http_session("x")     # s.get(url)               → api_call GET

* **A client constructor** — a client class (read through the file's imports:
  ``import requests as rq``, ``from httpx import Client``), a function of this file whose return
  annotation is one, or a factory whose name says it makes an HTTP session or client
  (``get_http_session``, ``create_http_client``).
* **Bound to a client** — a name (or ``self.<name>``) assigned a constructor call (awaited or
  not) or another client name, the target of ``with <constructor>(…) as name``, or annotated
  with a client class (``X | None`` and ``Optional[X]`` read as ``X``). A name
  the file also binds to another call or annotates with another type is not one: the set is per
  file, and the same name holding something else elsewhere would be a guess.
* **A call of a typed factory** as the receiver (``self._session().get(url)``).
* **An alias of the library** (``import requests as rq`` → ``rq.Session().delete(url)``).
* **An untyped ``…session`` receiver** (``def get(session, url): session.get(url)``) is one only
  in a file that imports an HTTP library and no SQL ORM, and never as ``db.session``, the
  Flask-SQLAlchemy session.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ..detection.api_calls import _ends_with_word
from ..treesitter import node_text

#: Fully qualified HTTP-client session classes (and ``requests.session()``, the factory).
_CLIENT_CLASSES = frozenset(
    {
        "requests.Session",
        "requests.session",
        "requests.sessions.Session",
        "httpx.Client",
        "httpx.AsyncClient",
        "aiohttp.ClientSession",
        "aiohttp.client.ClientSession",
    }
)
#: Top-level modules whose import makes an untyped ``session`` receiver an HTTP session.
_HTTP_MODULES = frozenset({"requests", "httpx", "aiohttp"})
#: Top-level modules of a SQL ORM whose sessions share the name.
_ORM_MODULES = frozenset({"sqlalchemy", "flask_sqlalchemy", "sqlmodel"})
#: A factory named for an HTTP session or client: ``get_http_session``, ``createHttpClient``.
_HTTP_FACTORY = re.compile(r"(?:^|_)http_?(?:session|client)$", re.IGNORECASE)
_OPTIONAL = re.compile(r"(?:typing\.)?Optional\[([\w.]+)\]|([\w.]+)\|None|None\|([\w.]+)")
_LITERALS = ("none", "string", "integer", "float", "true", "false")


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _aliases(root: Node, source: bytes) -> dict[str, str]:
    """Local name → the dotted name it stands for, from the file's imports."""
    out: dict[str, str] = {}
    for node in _walk(root):
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "aliased_import":
                    name, alias = (
                        child.child_by_field_name("name"),
                        child.child_by_field_name("alias"),
                    )
                    if name is not None and alias is not None:
                        out[node_text(alias, source)] = node_text(name, source)
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None or module.type != "dotted_name":
                continue  # a relative import names no library
            prefix = node_text(module, source)
            for child in node.children_by_field_name("name"):
                if child.type == "dotted_name":
                    text = node_text(child, source)
                    out[text] = f"{prefix}.{text}"
                elif child.type == "aliased_import":
                    name, alias = (
                        child.child_by_field_name("name"),
                        child.child_by_field_name("alias"),
                    )
                    if name is not None and alias is not None:
                        out[node_text(alias, source)] = f"{prefix}.{node_text(name, source)}"
    return out


def _qualified(expr: Node, source: bytes, aliases: dict[str, str]) -> str:
    """``expr``'s dotted text with its first name read through the imports. A string annotation
    is read without its quotes, an optional one (``X | None``, ``Optional[X]``) as ``X``."""
    text = "".join(node_text(expr, source).split()).strip("'\"")
    optional = _OPTIONAL.fullmatch(text)
    if optional is not None:
        text = next(g for g in optional.groups() if g)
    head, _, rest = text.partition(".")
    head = aliases.get(head, head)
    return f"{head}.{rest}" if rest else head


def _typed_factories(root: Node, source: bytes, aliases: dict[str, str]) -> set[str]:
    """Names of this file's functions whose return annotation is a client class."""
    out: set[str] = set()
    for node in _walk(root):
        if node.type != "function_definition":
            continue
        name, returns = node.child_by_field_name("name"), node.child_by_field_name("return_type")
        if (
            name is not None
            and returns is not None
            and _qualified(returns, source, aliases) in _CLIENT_CLASSES
        ):
            out.add(node_text(name, source))
    return out


def _factory_name(fn: Node, source: bytes) -> str | None:
    """The function a call names, when it is ``name``, ``self.name`` or ``cls.name``."""
    if fn.type == "identifier":
        return node_text(fn, source)
    if fn.type == "attribute":
        obj, attr = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
        if obj is not None and attr is not None and node_text(obj, source) in ("self", "cls"):
            return node_text(attr, source)
    return None


def _constructs_client(
    fn: Node, source: bytes, aliases: dict[str, str], factories: set[str]
) -> bool:
    """Whether a call of ``fn`` returns an HTTP client (see the module docstring)."""
    if _qualified(fn, source, aliases) in _CLIENT_CLASSES:
        return True
    if _factory_name(fn, source) in factories:
        return True
    last = node_text(fn, source).rsplit(".", 1)[-1]
    return bool(_HTTP_FACTORY.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", last)))


def _target(node: Node | None, source: bytes) -> str | None:
    """A bound name: an identifier or ``self.<name>``; anything else binds no single name."""
    if node is None:
        return None
    if node.type == "identifier":
        return node_text(node, source)
    if node.type == "attribute":
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        if obj is not None and attr is not None and node_text(obj, source) == "self":
            return f"self.{node_text(attr, source)}"
    return None


def _bindings(
    root: Node, source: bytes, aliases: dict[str, str], factories: set[str]
) -> tuple[set[str], set[str]]:
    """``(names bound to an HTTP client, names bound to something else)``."""
    clients: set[str] = set()
    others: set[str] = set()
    copies: list[tuple[str, str]] = []

    def bind(name: str | None, value: Node | None, annotation: Node | None) -> None:
        if name is None:
            return
        if annotation is not None:
            (clients if _qualified(annotation, source, aliases) in _CLIENT_CLASSES else others).add(
                name
            )
        if value is not None and value.type == "await":
            value = value.named_children[0] if value.named_children else None
        if value is None or value.type in _LITERALS:
            return  # ``self.session = None`` before the real one, a default value
        if value.type in ("identifier", "attribute"):
            copies.append((name, "".join(node_text(value, source).split())))
            return  # ``session = self._session``: what the copied name holds, read below
        fn = value.child_by_field_name("function") if value.type == "call" else None
        if fn is not None and _constructs_client(fn, source, aliases, factories):
            clients.add(name)
        else:
            others.add(name)

    for node in _walk(root):
        if node.type == "assignment":
            bind(
                _target(node.child_by_field_name("left"), source),
                node.child_by_field_name("right"),
                node.child_by_field_name("type"),
            )
        elif (
            node.type == "as_pattern"
            and node.parent is not None
            and node.parent.type == "with_item"
        ):
            value = node.named_children[0] if node.named_children else None
            alias = node.child_by_field_name("alias")
            target = (
                alias.named_children[0] if alias is not None and alias.named_children else alias
            )
            bind(_target(target, source), value, None)
        elif node.type in ("typed_parameter", "typed_default_parameter"):
            name = node.child_by_field_name("name")
            if name is None:
                name = next((c for c in node.named_children if c.type == "identifier"), None)
            bind(_target(name, source), None, node.child_by_field_name("type"))
    for name, copied in copies:
        (clients if copied in clients and copied not in others else others).add(name)
    return clients, others


def _untyped_sessions(root: Node, source: bytes, others: set[str]) -> set[str]:
    """Receivers of calls whose last name is a ``…session`` (see the module docstring)."""
    out: set[str] = set()
    for node in _walk(root):
        if node.type != "call":
            continue
        fn = node.child_by_field_name("function")
        obj = (
            fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None
        )
        if obj is None or obj.type not in ("identifier", "attribute"):
            continue
        receiver = "".join(node_text(obj, source).split())
        parts = receiver.split(".")
        last = parts[-1]
        if not (
            _ends_with_word(last, "session") or _ends_with_word(last.replace("_", ""), "session")
        ):
            continue
        if len(parts) > 1 and parts[-2].lower().endswith("db"):
            continue  # db.session: Flask-SQLAlchemy
        if receiver not in others:
            out.add(receiver)
    return out


def collect_http_client_ids(
    root: Node, source: bytes, external_imports: list[str]
) -> frozenset[str]:
    """The file's HTTP-client names, for ``set_http_client_ids``."""
    aliases = _aliases(root, source)
    factories = _typed_factories(root, source, aliases)
    clients, others = _bindings(root, source, aliases, factories)
    ids = clients - others
    # a typed factory's call as the receiver: ``self._session().get(url)`` reads as ``self._session``
    ids |= {f for f in factories if f not in others} | {f"self.{f}" for f in factories}
    # ``import requests as rq``: the alias names the library, as ``requests`` itself does
    ids |= {
        alias
        for alias, target in aliases.items()
        if target in _HTTP_MODULES and alias not in others
    }
    tops = {i.split(".", 1)[0] for i in external_imports}
    if tops & _HTTP_MODULES and not tops & _ORM_MODULES:
        ids |= _untyped_sessions(root, source, others)
    return frozenset(ids)
