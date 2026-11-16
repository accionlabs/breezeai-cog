"""PHP data access (``db_method_call``) — recorded only where the code proves it.

The shared classifier matches calls by name (``findMany`` → Prisma, a ``…builder`` receiver →
CodeIgniter), which in PHP turns DTO factories into database calls and Doctrine into TypeORM.
For PHP it classifies no data access at all; one additive detector per library does, from
evidence (``php/receivers.py``), with this shared loop:

====================  ===========================================================================
Hint                  Proof (detector)
====================  ===========================================================================
``eloquent``          a static call on a class the ``php.class-ancestors`` fact shows extends an
                      Eloquent model (``User::create([...])``, ``Song::query()->get()``), or an
                      instance call on a variable / property typed or constructed as one
                      (``php_laravel/data_access.py``)
``doctrine``          a call on a receiver typed as Doctrine's ``EntityManagerInterface`` /
                      ``ObjectManager`` / ``ManagerRegistry``; an inherited repository method on
                      ``$this`` in a ``ServiceEntityRepository`` / ``EntityRepository`` subclass,
                      or on a variable typed as one (``php_doctrine/data_access.py``)
``codeigniter-builder``  ``$this->db`` in a CodeIgniter file, ``db_connect()``,
                      ``\\Config\\Database::connect()``, a variable assigned from one, or a variable
                      typed / constructed as a ``CodeIgniter\\Model`` subclass
                      (``php_codeigniter/data_access.py``)
(no hint)             the imported ``Illuminate\\Support\\Facades\\DB`` facade (the query builder,
                      not Eloquent models) (``php_laravel/data_access.py``)
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

from collections.abc import Mapping

from tree_sitter import Node

from .additive import DetectContext, Detection
from .emits import Emit
from .php.mappings import CONTROL_FLOW, EMIT_TYPES
from .php.receivers import Rules, method_name, outermost_calls
from .treesitter import node_text

#: kind → (data methods, dataAccessHint).
Kinds = Mapping[str, tuple[set[str], str | None]]


def _in_control_flow(call: Node) -> bool:
    """Whether the call's own statement is a control-flow statement (``if (DB::table…->exists())``):
    a header keeps no semantic type, so the call gets a record of its own."""
    node = call.parent
    while node is not None and node.type not in EMIT_TYPES:
        node = node.parent
    return node is not None and node.type in CONTROL_FLOW


def detect_data_access(dc: DetectContext, rules: Rules, kinds: Kinds) -> Detection | None:
    """A ``db_method_call`` for every outermost chain on a receiver of one of ``rules``' kinds
    that calls a data method of that kind."""
    found: list[Emit] = []
    for call, calls, kind in outermost_calls(dc, rules):
        methods, hint = kinds[kind]
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
