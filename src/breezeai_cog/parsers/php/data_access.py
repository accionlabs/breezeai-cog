"""PHP data access (``db_method_call``) — recorded only where the code proves it.

The shared classifier matches calls by name (``findMany`` → Prisma, a ``…builder`` receiver →
CodeIgniter), which in PHP turns DTO factories into database calls and Doctrine into TypeORM.
For PHP it classifies no data access at all; this additive detector does, from evidence:

====================  ===========================================================================
Hint                  Proof
====================  ===========================================================================
``eloquent``          a static call on a class the ``php.model-classes`` fact knows extends an
                      Eloquent model (``User::create([...])``, ``Song::query()->get()``), or an
                      instance call on a variable / property typed or constructed as one
``doctrine``          a call on a receiver typed as Doctrine's ``EntityManagerInterface`` /
                      ``ObjectManager`` / ``ManagerRegistry``; an inherited repository method on
                      ``$this`` in a ``ServiceEntityRepository`` / ``EntityRepository`` subclass,
                      or on a variable typed as one
``codeigniter-builder``  ``$this->db`` in a CodeIgniter file, ``db_connect()``,
                      ``\\Config\\Database::connect()``, a variable assigned from one, or a variable
                      typed / constructed as a ``CodeIgniter\\Model`` subclass
(no hint)             the imported ``Illuminate\\Support\\Facades\\DB`` facade (the query builder,
                      not Eloquent models)
====================  ===========================================================================

A chain (``Song::query()->where(...)->get()``) is one record, at its outermost call, and only
when one of its methods is a data method of that kind. A static model call must itself start a
query (``Song::search()`` — Laravel Scout — and ``User::factory()`` do not).
A statement the shared classifier already filed as a raw SQL ``query_statement`` keeps that.
Calls on untyped variables, custom repository methods, Redis / Mongo / PDO are gaps, never
guesses.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from tree_sitter import Node

from ...emit import statement_id
from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector, register_fact,
)
from ..emits import Emit
from ..php_route_common import PHP_CALL_TYPES, unwrap
from ..treesitter import node_text
from .imports import resolve_class, use_aliases
from .index import namespace_of
from .mappings import EMIT_TYPES

FACT = "php.model-classes"

#: Framework base classes → the kind of class that extends them.
_BASES = {
    "Illuminate\\Database\\Eloquent\\Model": "eloquent",
    "Illuminate\\Foundation\\Auth\\User": "eloquent",
    "Illuminate\\Database\\Eloquent\\Relations\\Pivot": "eloquent",
    "Illuminate\\Database\\Eloquent\\Relations\\MorphPivot": "eloquent",
    "CodeIgniter\\Model": "ci-model",
    "Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository": "doctrine-repo",
    "Doctrine\\ORM\\EntityRepository": "doctrine-repo",
}
#: Types that are a database handle themselves.
_HANDLES = {
    "Doctrine\\ORM\\EntityManagerInterface": "doctrine-em",
    "Doctrine\\ORM\\EntityManager": "doctrine-em",
    "Doctrine\\Persistence\\ObjectManager": "doctrine-em",
    "Doctrine\\Common\\Persistence\\ObjectManager": "doctrine-em",
    "Doctrine\\Persistence\\ManagerRegistry": "doctrine-em",
    "CodeIgniter\\Database\\BaseConnection": "ci-db",
    "CodeIgniter\\Database\\ConnectionInterface": "ci-db",
    "CodeIgniter\\Database\\BaseBuilder": "ci-db",
}
_DB_FACADE = "Illuminate\\Support\\Facades\\DB"

_ELOQUENT_QUERY = {
    "query", "where", "wherein", "wherenotin", "wherenull", "wherenotnull", "wherebetween",
    "wherehas", "wheredoesnthave", "wherekey", "orwhere", "find", "findmany", "findorfail",
    "findornew", "first", "firstorfail", "firstwhere", "firstorcreate", "firstornew",
    "updateorcreate", "create", "createmany", "forcecreate", "insert", "insertorignore",
    "insertgetid", "upsert", "destroy", "all", "get", "count", "exists", "doesntexist", "pluck",
    "paginate", "simplepaginate", "cursorpaginate", "select", "with", "withcount", "has",
    "doesnthave", "orderby", "orderbydesc", "latest", "oldest", "limit", "take", "chunk",
    "chunkbyid", "cursor", "lazy", "lazybyid", "sum", "avg", "max", "min", "increment",
    "decrement", "truncate", "withtrashed", "onlytrashed", "update", "delete", "value", "sole",
}
_ELOQUENT_INSTANCE = {
    "save", "savequietly", "update", "updatequietly", "delete", "deletequietly", "forcedelete",
    "restore", "refresh", "fresh", "push", "increment", "decrement", "touch", "load",
    "loadmissing", "loadcount",
}
_DB_FACADE_METHODS = {
    "table", "select", "selectone", "insert", "update", "delete", "statement", "unprepared",
    "affectingstatement", "scalar", "query",
}
_DOCTRINE_EM = {
    "persist", "remove", "flush", "find", "createquerybuilder", "createquery", "createnativequery",
    "refresh", "lock", "transactional", "wrapintransaction",
}
_DOCTRINE_REPO = {"find", "findall", "findby", "findoneby", "count", "createquerybuilder", "matching"}
_CI_BUILDER = {
    "table", "builder", "query", "simplequery", "get", "getwhere", "insert", "insertbatch",
    "update", "updatebatch", "delete", "replace", "countall", "countallresults", "emptytable",
    "truncate", "select", "where", "wherein", "orwhere", "like", "join", "orderby", "groupby",
    "limit", "set",
}
_CI_MODEL = _CI_BUILDER | {"find", "findall", "first", "save", "findcolumn", "paginate", "chunk"}

#: kind → (data methods, dataAccessHint).
_KINDS: dict[str, tuple[set[str], str | None]] = {
    "eloquent": (_ELOQUENT_QUERY | _ELOQUENT_INSTANCE, "eloquent"),
    "eloquent-instance": (_ELOQUENT_INSTANCE, "eloquent"),
    # The facade starts a query builder, whose methods are Eloquent's builder methods.
    "db-facade": (_DB_FACADE_METHODS | _ELOQUENT_QUERY, None),
    "doctrine-em": (_DOCTRINE_EM | _DOCTRINE_REPO, "doctrine"),
    "doctrine-repo": (_DOCTRINE_REPO, "doctrine"),
    "ci-db": (_CI_BUILDER, "codeigniter-builder"),
    "ci-model": (_CI_MODEL, "codeigniter-builder"),
}

_SCOPES = {"function_definition", "method_declaration", "anonymous_function", "arrow_function"}
_CLASSES = {"class_declaration", "trait_declaration", "enum_declaration"}


# ── the fact: which repo classes are models / repositories ───────────────────────────────────

def _collect(root: Node, source: bytes, rel: str, repo_root: Any) -> list[tuple[str, str]] | None:
    """``(class, parent)`` fully qualified, for every class of the file that extends one."""
    aliases, namespace = use_aliases(root, source), namespace_of(root, source)
    found: list[tuple[str, str]] = []

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == "class_declaration":
                name = child.child_by_field_name("name")
                base = next((c for c in child.named_children if c.type == "base_clause"), None)
                parent_node = next(iter(base.named_children), None) if base is not None else None
                parent = (resolve_class(node_text(parent_node, source), aliases, namespace)
                          if parent_node is not None else None)
                if name is not None and parent:
                    own = node_text(name, source)
                    found.append((f"{namespace}\\{own}" if namespace else own, parent))
            visit(child)

    visit(root)
    return found or None


def _resolve(values: list[list[tuple[str, str]]], index: Any) -> dict[str, str] | None:
    """class → kind, through any depth of repo parents. A class declared twice with different
    parents is ambiguous and left out."""
    parents: dict[str, str | None] = {}
    for file_values in values:
        for cls, parent in file_values:
            parents[cls] = parent if parents.get(cls, parent) == parent else None
    kinds: dict[str, str] = {}
    changed = True
    while changed:  # order-independent fixed point
        changed = False
        for cls, known_parent in parents.items():
            if cls in kinds or known_parent is None:
                continue
            kind = _BASES.get(known_parent) or kinds.get(known_parent)
            if kind is not None:
                kinds[cls] = kind
                changed = True
    return kinds or None


register_fact(FactSpec(name=FACT, language="php", gate=(b"extends",),
                       collect=_collect, resolve=_resolve))


# ── the detector ─────────────────────────────────────────────────────────────────────────────

def _type_name(node: Node | None, source: bytes) -> str | None:
    """The single class a declared type names (``?Foo`` → ``Foo``); None for unions etc."""
    node = node.named_children[0] if node is not None and node.type == "optional_type" else node
    if node is None or node.type != "named_type":
        return None
    return node_text(node, source)


class _Context:
    def __init__(self, dc: DetectContext) -> None:
        self.source = dc.source
        self.root = dc.root
        self.aliases = use_aliases(dc.root, dc.source)
        self.namespace = namespace_of(dc.root, dc.source)
        self.models: dict[str, str] = index_fact(dc.ctx.resolution_index, FACT) or {}
        self.ci_file = b"CodeIgniter\\" in dc.source

    def kind_of_type(self, written: str | None, *, instance: bool) -> str | None:
        fqcn = resolve_class(written, self.aliases, self.namespace) if written else None
        if fqcn is None:
            return None
        kind = _HANDLES.get(fqcn) or self.models.get(fqcn)
        return "eloquent-instance" if kind == "eloquent" and instance else kind

    def class_fqcn(self, node: Node) -> str | None:
        name = node.child_by_field_name("name")
        if name is None:
            return None
        own = node_text(name, self.source)
        return f"{self.namespace}\\{own}" if self.namespace else own


def _method(call: Node, source: bytes) -> str:
    name = call.child_by_field_name("name")
    if name is None and call.type == "function_call_expression":
        name = call.child_by_field_name("function")
    return node_text(name, source).rsplit("\\", 1)[-1].lower() if name is not None else ""


def _chain(call: Node) -> tuple[Node, list[Node]]:
    """(base expression, calls from the innermost to ``call``)."""
    calls = [call]
    node = call
    while True:
        receiver = unwrap(node.child_by_field_name("object") or node.child_by_field_name("scope"))
        if receiver is not None and receiver.type in PHP_CALL_TYPES - {"function_call_expression"}:
            calls.append(receiver)
            node = receiver
            continue
        calls.reverse()
        return (receiver if receiver is not None else node), calls


def _outermost(call: Node) -> bool:
    parent = call.parent
    while parent is not None and parent.type == "parenthesized_expression":
        parent = parent.parent
    if parent is None or parent.type not in PHP_CALL_TYPES:
        return True
    receiver = unwrap(parent.child_by_field_name("object") or parent.child_by_field_name("scope"))
    return receiver is None or (receiver.start_byte, receiver.end_byte) != (
        call.start_byte, call.end_byte)


def _base_kind(base: Node, calls: list[Node], cx: _Context, scope: dict[str, str],
               props: dict[str, str], this_kind: str | None) -> str | None:
    """The kind of database handle a chain starts from, or None."""
    source = cx.source
    first = calls[0]
    scope_node = first.child_by_field_name("scope") if first.type == "scoped_call_expression" else None
    if scope_node is not None and (base.start_byte, base.end_byte) == (
            scope_node.start_byte, scope_node.end_byte):
        written = node_text(base, source)
        fqcn = resolve_class(written, cx.aliases, cx.namespace)
        if fqcn == _DB_FACADE:
            # Only a call that starts a query (table / select / insert / statement …).
            return "db-facade" if _method(first, source) in _DB_FACADE_METHODS else None
        if fqcn == "Config\\Database" and _method(first, source) == "connect":
            return "ci-db"
        if fqcn is None or cx.models.get(fqcn) != "eloquent":
            return None
        # The static call must itself start a query: Scout's ``Song::search(...)`` hits a search
        # engine, ``User::observe(...)`` registers an observer.
        return "eloquent" if _method(first, source) in _ELOQUENT_QUERY else None
    if first.type == "function_call_expression":
        return "ci-db" if _method(first, source) == "db_connect" else None
    if base.type == "variable_name":
        name = node_text(base, source)
        if name == "$this":
            if this_kind != "doctrine-repo":
                return None
            return "doctrine-em" if _method(first, source) == "getentitymanager" else this_kind
        return scope.get(name)
    if base.type == "member_access_expression":
        owner = base.child_by_field_name("object")
        prop = base.child_by_field_name("name")
        if owner is None or prop is None or node_text(owner, source) != "$this":
            return None
        prop_name = node_text(prop, source)
        if prop_name == "db" and cx.ci_file:
            return "ci-db"
        return props.get(prop_name)
    return None


def _assigned_kind(value: Node, cx: _Context, scope: dict[str, str], props: dict[str, str],
                   this_kind: str | None) -> str | None:
    """The handle kind an assignment's right-hand side yields (``new UserModel()``,
    ``$this->db->table('x')``, ``$em->getRepository(Post::class)``)."""
    value = unwrap(value) or value
    if value.type == "object_creation_expression":
        cls = next((c for c in value.named_children if c.type in {"name", "qualified_name"}), None)
        return cx.kind_of_type(node_text(cls, cx.source), instance=True) if cls is not None else None
    if value.type in PHP_CALL_TYPES:
        base, calls = _chain(value)
        kind = _base_kind(base, calls, cx, scope, props, this_kind)
        if kind in {"ci-db"}:
            return kind
        if kind == "doctrine-em" and _method(calls[-1], cx.source) == "getrepository":
            return "doctrine-repo"
    return None


def _params(fn: Node, cx: _Context) -> dict[str, str]:
    out: dict[str, str] = {}
    params = fn.child_by_field_name("parameters")
    for param in params.named_children if params is not None else []:
        name = param.child_by_field_name("name")
        kind = cx.kind_of_type(_type_name(param.child_by_field_name("type"), cx.source), instance=True)
        if name is not None and kind is not None:
            out[node_text(name, cx.source)] = kind
    return out


def _properties(cls: Node, cx: _Context) -> dict[str, str]:
    """``$this->x`` → kind, from typed properties and promoted constructor parameters."""
    out: dict[str, str] = {}
    body = cls.child_by_field_name("body")
    for member in body.named_children if body is not None else []:
        if member.type == "property_declaration":
            kind = cx.kind_of_type(_type_name(member.child_by_field_name("type"), cx.source),
                                   instance=True)
            for element in member.named_children:
                name = element.child_by_field_name("name") if element.type == "property_element" else None
                if kind is not None and name is not None:
                    out[node_text(name, cx.source).lstrip("$")] = kind
        elif member.type == "method_declaration":
            params = member.child_by_field_name("parameters")
            for param in params.named_children if params is not None else []:
                if param.type != "property_promotion_parameter":
                    continue
                name = param.child_by_field_name("name")
                kind = cx.kind_of_type(_type_name(param.child_by_field_name("type"), cx.source),
                                       instance=True)
                if kind is not None and name is not None:
                    out[node_text(name, cx.source).lstrip("$")] = kind
    return out


def _local_kinds(fn: Node, cx: _Context, inherited: dict[str, str], props: dict[str, str],
                 this_kind: str | None) -> dict[str, str]:
    """Variable → kind inside one function: its typed parameters plus assignments in its own
    body. A variable assigned two different kinds (or a non-handle) is dropped."""
    scope = {**inherited, **_params(fn, cx)}
    assigned: dict[str, str | None] = {}
    for node in _own_nodes(fn.child_by_field_name("body")):
        if node.type != "assignment_expression":
            continue
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or left.type != "variable_name":
            continue
        name = node_text(left, cx.source)
        kind = _assigned_kind(right, cx, scope, props, this_kind)
        assigned[name] = kind if assigned.get(name, kind) == kind else None
    for name, kind in assigned.items():
        if kind is None:
            scope.pop(name, None)
        else:
            scope[name] = kind
    return scope


def _own_nodes(node: Node | None) -> Iterator[Node]:
    """Every node of a function body, not descending into nested functions or classes."""
    if node is None:
        return
    for child in node.named_children:
        yield child
        if child.type not in _SCOPES | _CLASSES:
            yield from _own_nodes(child)


def _statement_of(node: Node) -> Node | None:
    current: Node | None = node
    while current is not None and current.type not in EMIT_TYPES:
        current = current.parent
    return current


def detect_php_data_access(dc: DetectContext) -> Detection | None:
    cx = _Context(dc)
    if not cx.models and not any(marker in dc.source for marker in (
            b"Illuminate\\", b"Doctrine\\", b"CodeIgniter\\", b"db_connect", b"Config\\Database")):
        return None
    raw_sql = {s.id for s in dc.record.statements if s.semanticType == "query_statement"}
    found: list[Emit] = []

    def visit(node: Node, scope: dict[str, str], props: dict[str, str], this_kind: str | None) -> None:
        for child in node.named_children:
            if child.type in _CLASSES:
                fqcn = cx.class_fqcn(child)
                visit(child, {}, _properties(child, cx), cx.models.get(fqcn) if fqcn else None)
                continue
            if child.type in _SCOPES:
                inherited = scope if child.type in {"anonymous_function", "arrow_function"} else {}
                visit(child, _local_kinds(child, cx, inherited, props, this_kind), props, this_kind)
                continue
            if child.type in PHP_CALL_TYPES and _outermost(child):
                _classify(child, scope, props, this_kind)
            visit(child, scope, props, this_kind)

    def _classify(call: Node, scope: dict[str, str], props: dict[str, str], this_kind: str | None) -> None:
        base, calls = _chain(call)
        kind = _base_kind(base, calls, cx, scope, props, this_kind)
        if kind is None:
            return
        methods, hint = _KINDS[kind]
        data = [c for c in calls if _method(c, cx.source) in methods]
        if not data:
            return
        statement = _statement_of(call)
        if statement is not None and statement_id(
                dc.path, statement.start_point[0] + 1, statement.start_point[1]) in raw_sql:
            return  # raw SQL already filed as query_statement
        name = data[-1].child_by_field_name("name")
        found.append(Emit(call, {
            "semanticType": "db_method_call",
            "method": node_text(name, cx.source) if name is not None else None,
            "dataAccessHint": hint,
        }, enrich="own"))

    visit(dc.root, {}, {}, None)
    return Detection(found) if found else None


register_detector(Detector(
    name="php-data-access",
    gate=Gate("php", order=40),
    detect=detect_php_data_access,
))
