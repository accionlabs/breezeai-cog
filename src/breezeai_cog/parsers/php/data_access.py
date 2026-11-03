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
A proven data call wins over the shared classifier's raw-SQL ``query_statement`` on the same
statement (``DB::select('SELECT …')`` is a ``db_method_call``; the SQL stays in ``text``), as in Go
and as the shared classifier itself ranks a classified call above SQL text.
Calls on untyped variables, custom repository methods, Redis / Mongo / PDO are gaps, never
guesses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tree_sitter import Node

from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector, register_fact,
)
from ..emits import Emit
from ..treesitter import node_text
from .imports import resolve_class, use_aliases
from .index import namespace_of
from .mappings import CONTROL_FLOW, EMIT_TYPES
from .receivers import Rules, method_name, outermost_calls

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

def _static(models: dict[str, str]) -> Callable[[str, str], str | None]:
    def kind(fqcn: str, method: str) -> str | None:
        if fqcn == _DB_FACADE:
            # Only a call that starts a query (table / select / insert / statement …).
            return "db-facade" if method in _DB_FACADE_METHODS else None
        if fqcn == "Config\\Database":
            return "ci-db" if method == "connect" else None
        # A static model call must itself start a query: Scout's ``Song::search(...)`` hits a
        # search engine, ``User::observe(...)`` registers an observer.
        return "eloquent" if models.get(fqcn) == "eloquent" and method in _ELOQUENT_QUERY else None
    return kind


def _this_call(class_kind: str | None, method: str) -> str | None:
    if class_kind != "doctrine-repo":
        return None
    return "doctrine-em" if method == "getentitymanager" else class_kind


def _chain_result(kind: str, methods: list[str]) -> str | None:
    """``$builder = $this->db->table('x')`` is a builder; ``$repo = $em->getRepository(X::class)``
    a repository. Other chain results are values, not handles."""
    if kind == "ci-db":
        return kind
    return "doctrine-repo" if kind == "doctrine-em" and methods[-1] == "getrepository" else None


def _rules(dc: DetectContext, models: dict[str, str]) -> Rules:
    ci_file = b"CodeIgniter\\" in dc.source
    return Rules(
        types={**models, **_HANDLES},
        static=_static(models),
        function=lambda name: "ci-db" if name == "db_connect" else None,
        this_property=lambda name: "ci-db" if name == "db" and ci_file else None,
        this_call=_this_call,
        chain_result=_chain_result,
        instance=lambda kind: "eloquent-instance" if kind == "eloquent" else kind,
    )


def _in_control_flow(call: Node) -> bool:
    """Whether the call's own statement is a control-flow statement (``if (DB::table…->exists())``):
    a header keeps no semantic type, so the call gets a record of its own."""
    node = call.parent
    while node is not None and node.type not in EMIT_TYPES:
        node = node.parent
    return node is not None and node.type in CONTROL_FLOW


def detect_php_data_access(dc: DetectContext) -> Detection | None:
    models: dict[str, str] = index_fact(dc.ctx.resolution_index, FACT) or {}
    if not models and not any(marker in dc.source for marker in (
            b"Illuminate\\", b"Doctrine\\", b"CodeIgniter\\", b"db_connect", b"Config\\Database")):
        return None
    found: list[Emit] = []
    for call, calls, kind in outermost_calls(dc, _rules(dc, models)):
        methods, hint = _KINDS[kind]
        data = [c for c in calls if method_name(c, dc.source) in methods]
        if not data:
            continue
        name = data[-1].child_by_field_name("name")
        found.append(Emit(call, {
            "semanticType": "db_method_call",
            "method": node_text(name, dc.source) if name is not None else None,
            "dataAccessHint": hint,
        }, enrich="never" if _in_control_flow(call) else "own",
            reclassify=frozenset({"query_statement"})))
    return Detection(found) if found else None


register_detector(Detector(
    name="php-data-access",
    gate=Gate("php", order=40),
    detect=detect_php_data_access,
))
