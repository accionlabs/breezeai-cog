"""The names in a Python file that are HTTP clients or SQLAlchemy sessions, for the shared classifier.

``session`` is no HTTP-client name and no database name on its own: ``requests.Session``,
SQLAlchemy's ``Session`` and other libraries' sessions share it, so ``session.delete(x)`` is an
HTTP ``DELETE`` in one file and a row deleted in another. A receiver is one of them only where
the file shows it::

    import requests, httpx, aiohttp
    session = requests.Session()                 # session.delete(url)      → api_call DELETE
    self.http = httpx.Client()                   # self.http.get(url)       → api_call GET
    with httpx.Client() as client: ...           # client.delete(url)       → api_call DELETE
    async with aiohttp.ClientSession() as s: ... # s.get(url)               → api_call GET
    def fetch(session: requests.Session): ...    # session.get(url)         → api_call GET
    def _session(self) -> httpx.Client: ...      # self._session().get(url) → api_call GET
    s = await registry.get_http_session("x")     # s.get(url)               → api_call GET

    from sqlalchemy.orm import Session, sessionmaker
    def remove(session: Session, o): ...         # session.delete(o)        → db_method_call sqlalchemy
    with Session(engine) as s: ...               # s.delete(o)              → db_method_call sqlalchemy
    SessionLocal = sessionmaker(bind=engine)     # with SessionLocal() as s: s.delete(o) → sqlalchemy

* **A constructor** — a class of the kind (read through the file's imports:
  ``import requests as rq``, ``from httpx import Client``), a function of this file whose return
  annotation is one, a name bound to a ``sessionmaker(…)``, or a factory whose name says it makes
  an HTTP session or client (``get_http_session``, ``create_http_client``).
* **Bound to one** — a name (or ``self.<name>``) assigned a constructor call (awaited or not) or
  another such name, the target of ``with <constructor>(…) as name``, or annotated with a class
  of the kind (``X | None`` and ``Optional[X]`` read as ``X``). A name the file also binds to
  another call or annotates with another type is not one: the set is per file, and the same name
  holding something else elsewhere would be a guess.
* **A call of a typed HTTP factory** as the receiver (``self._session().get(url)``).
* **An alias of an HTTP library** (``import requests as rq`` → ``rq.Session().delete(url)``).
* **An untyped ``session``** (``def rm(session, o): session.delete(o)``) is neither, whatever the
  file imports: a library this module does not list can hold either, beside the ones it does.

The names go to the shared classifier whole (``self.session``, not ``session``), so another
object's ``request.session`` is not the file's SQLAlchemy ``session``. ``db.session``, the
Flask-SQLAlchemy session, is read by the classifier itself (``detection/db_queries.py``). Any other
``session`` is left unrecorded.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from tree_sitter import Node

from ..treesitter import node_text


@dataclass(frozen=True)
class _Kind:
    """What makes a name a client of one kind."""

    #: Fully qualified classes (or factory functions) whose call returns one, and whose
    #: annotation types one.
    classes: frozenset[str]
    #: Fully qualified factory makers: a name bound to their call constructs one when called.
    makers: frozenset[str] = frozenset()
    #: A function name that says it returns one, whatever the file imports.
    factory_name: re.Pattern[str] | None = None


_HTTP = _Kind(
    classes=frozenset(
        {
            "requests.Session",
            "requests.session",
            "requests.sessions.Session",
            "httpx.Client",
            "httpx.AsyncClient",
            "aiohttp.ClientSession",
            "aiohttp.client.ClientSession",
        }
    ),
    # ``get_http_session``, ``createHttpClient``
    factory_name=re.compile(r"(?:^|_)http_?(?:session|client)$", re.IGNORECASE),
)
_SQLALCHEMY = _Kind(
    classes=frozenset(
        {
            "sqlalchemy.orm.Session",
            "sqlalchemy.orm.session.Session",
            "sqlalchemy.orm.scoped_session",
            "sqlalchemy.orm.scoping.scoped_session",
            "sqlalchemy.ext.asyncio.AsyncSession",
            "sqlalchemy.ext.asyncio.session.AsyncSession",
            "sqlalchemy.ext.asyncio.async_scoped_session",
            # SQLModel's sessions are SQLAlchemy's, subclassed
            "sqlmodel.Session",
            "sqlmodel.orm.session.Session",
            "sqlmodel.ext.asyncio.session.AsyncSession",
        }
    ),
    makers=frozenset(
        {
            "sqlalchemy.orm.sessionmaker",
            "sqlalchemy.orm.session.sessionmaker",
            "sqlalchemy.ext.asyncio.async_sessionmaker",
            "sqlalchemy.ext.asyncio.session.async_sessionmaker",
        }
    ),
)
#: Top-level modules of the HTTP libraries above.
_HTTP_MODULES = frozenset({"requests", "httpx", "aiohttp"})
#: The ``dataAccessHint`` the shared classifier gives a call on one of the file's ORM sessions.
_ORM_HINT = "sqlalchemy"
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


def _factories(root: Node, source: bytes, aliases: dict[str, str], kind: _Kind) -> set[str]:
    """Names of this file's functions whose return annotation is a class of ``kind``, and of
    names bound to a call of one of its makers (``SessionLocal = sessionmaker(…)``)."""
    out: set[str] = set()
    for node in _walk(root):
        if node.type == "function_definition":
            name, returns = (
                node.child_by_field_name("name"),
                node.child_by_field_name("return_type"),
            )
            if (
                name is not None
                and returns is not None
                and _qualified(returns, source, aliases) in kind.classes
            ):
                out.add(node_text(name, source))
        elif node.type == "assignment" and kind.makers:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            fn = right.child_by_field_name("function") if right and right.type == "call" else None
            if (
                left is not None
                and left.type == "identifier"
                and fn is not None
                and _qualified(fn, source, aliases) in kind.makers
            ):
                out.add(node_text(left, source))
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


def _constructs(
    fn: Node, source: bytes, aliases: dict[str, str], kind: _Kind, factories: set[str]
) -> bool:
    """Whether a call of ``fn`` returns a client of ``kind`` (see the module docstring)."""
    if _qualified(fn, source, aliases) in kind.classes:
        return True
    if _factory_name(fn, source) in factories:
        return True
    if kind.factory_name is None:
        return False
    last = node_text(fn, source).rsplit(".", 1)[-1]
    return bool(kind.factory_name.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", last)))


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
    root: Node, source: bytes, aliases: dict[str, str], kind: _Kind, factories: set[str]
) -> tuple[set[str], set[str]]:
    """``(names bound to a client of kind, names bound to something else)``."""
    clients: set[str] = set()
    others: set[str] = set()
    copies: list[tuple[str, str]] = []

    def bind(name: str | None, value: Node | None, annotation: Node | None) -> None:
        if name is None:
            return
        if annotation is not None:
            typed = _qualified(annotation, source, aliases) in kind.classes
            (clients if typed else others).add(name)
        if value is not None and value.type == "await":
            value = value.named_children[0] if value.named_children else None
        if value is None or value.type in _LITERALS:
            return  # ``self.session = None`` before the real one, a default value
        if value.type in ("identifier", "attribute"):
            copies.append((name, "".join(node_text(value, source).split())))
            return  # ``session = self._session``: what the copied name holds, read below
        fn = value.child_by_field_name("function") if value.type == "call" else None
        if fn is not None and _constructs(fn, source, aliases, kind, factories):
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
    for copy, copied in copies:
        (clients if copied in clients and copied not in others else others).add(copy)
    return clients, others


def collect_clients(root: Node, source: bytes) -> tuple[frozenset[str], dict[str, str]]:
    """``(HTTP-client names, for set_http_client_ids; ORM-session receiver → hint, for
    set_datastore_receivers)`` of the file."""
    aliases = _aliases(root, source)

    http_factories = _factories(root, source, aliases, _HTTP)
    clients, http_others = _bindings(root, source, aliases, _HTTP, http_factories)
    http = clients - http_others
    # a typed factory's call as the receiver: ``self._session().get(url)`` reads as ``self._session``
    http |= {f for f in http_factories if f not in http_others}
    http |= {f"self.{f}" for f in http_factories}
    # ``import requests as rq``: the alias names the library, as ``requests`` itself does
    http |= {
        alias
        for alias, target in aliases.items()
        if target in _HTTP_MODULES and alias not in http_others
    }

    orm_factories = _factories(root, source, aliases, _SQLALCHEMY)
    sessions, orm_others = _bindings(root, source, aliases, _SQLALCHEMY, orm_factories)
    receivers = {name: _ORM_HINT for name in sessions - orm_others - http}
    return frozenset(http), receivers
