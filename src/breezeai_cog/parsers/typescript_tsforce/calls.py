"""Salesforce outbound calls through ts-force, for TypeScript. See ``_detect_tsforce``."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..index_common import ClassHeritage
from ..treesitter import node_text
from ..typescript_sdk_common import annotation_type, callee_chain, emit_outbound, walk_calls


# --- Salesforce (ts-force) -------------------------------------------------------------
# ts-force has NO client.<resource>.<op>() chain — it is a SOQL ORM. Every read funnels
# through ``RestObject.query<SObject>(SObject, qry)`` (also on generated ``Entity.retrieve``
# static methods), and writes are instance methods (``rec.insert()``/``update``/``delete``).
# The outbound target is the **SObject type** — carried in the call's generic ``<Account>`` or
# as the receiver of ``Account.retrieve(...)`` — which we recognise by the code's own
# ``extends RestObject`` inheritance (no hardcoded entity list). The generic ORM classifier
# grabs ``.query`` first as ``db_method_call``; we reclassify those to ``api_call`` since a
# SOQL query is an outbound call to Salesforce, not local data access.
_TSFORCE_MARKER = b"ts-force"
_TSFORCE_BASE = "RestObject"
_TSFORCE_QUERY_METHODS = {
    "query",
    "queryMore",
    "retrieve",
}  # read surface (SObject in <T>/receiver)
_TSFORCE_WRITE_METHODS = {"insert", "update", "delete"}  # instance writes on a RestObject
# ts-force composite/bulk-DML classes: ``new CompositeCollection().update(records)`` writes a
# whole array in one call. The SObject is the element type of the array argument, not the
# receiver (which is the collection), so these resolve the endpoint from the argument.
_TSFORCE_BULK_WRITERS = {"CompositeCollection"}


def _restobject_entities(record: FileRecord) -> set[str]:
    """SObject entity names = classes that ``extends RestObject`` (the base parser already
    captured ``Class.extends``). Derived from the code's own inheritance — no hardcoded list."""
    return {c.name for c in record.classes if c.extends == _TSFORCE_BASE}


def _repo_entities(class_heritage: dict[str, ClassHeritage | None] | None) -> set[str]:
    """Repo-wide SObject entity names = every class the heritage index records as directly
    ``extends RestObject``. This is the same ``extends``-based rule as ``_restobject_entities``,
    lifted to the whole repo so a read call site can resolve an entity defined in another file.
    A simple name the index collapsed to ``None`` (declared with conflicting bases in >1 file)
    is skipped — honest: an ambiguous name is not a confirmed entity."""
    if not class_heritage:
        return set()
    return {n for n, ch in class_heritage.items() if ch is not None and ch.extends == _TSFORCE_BASE}


def _entity_typed_vars(root: Node, source: bytes, types: set[str]) -> dict[str, str]:
    """Local identifiers whose type resolves to one of ``types`` → that type name. Recognizes
    a variable assigned ``new T(...)`` and a variable/parameter annotated ``: T``. Used both to
    resolve a write receiver to its SObject (``rec.insert()`` where ``types`` = the entities)
    and to resolve a bulk-writer receiver (``bulk.update()`` where ``types`` =
    :data:`_TSFORCE_BULK_WRITERS`); a receiver not found here is left uncaptured (honest — we do
    not guess the instance's type). Sibling of :func:`client_identifiers` (which resolves SDK
    *client* types, incl. factory returns)."""
    out: dict[str, str] = {}

    def go(n: Node) -> None:
        if n.type == "required_parameter":  # (rec: Account)
            t = annotation_type(n, source)
            if t in types:
                name = n.child_by_field_name("pattern") or (
                    n.named_children[0] if n.named_children else None
                )
                if name is not None and name.type == "identifier":
                    out[node_text(name, source)] = t
        elif n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                t = annotation_type(n, source)
                if t in types:  # let rec: Account
                    out[node_text(name, source)] = t
                else:
                    v = n.child_by_field_name("value")
                    while v is not None and v.type in (
                        "await_expression",
                        "parenthesized_expression",
                    ):
                        v = v.named_children[0] if v.named_children else None
                    if v is not None and v.type == "new_expression":  # const rec = new Account()
                        ctor = v.child_by_field_name("constructor")
                        if ctor is not None and node_text(ctor, source) in types:
                            out[node_text(name, source)] = node_text(ctor, source)
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _array_element_type(node: Node, source: bytes) -> str | None:
    """Element type name of a ``: Type[]`` annotation on ``node`` (``accounts: Account[]`` →
    ``Account``), else None. Only the ``T[]`` array syntax is unwrapped; ``Array<T>`` and other
    shapes return None (honest — not resolved rather than guessed)."""
    ann = next((c for c in node.named_children if c.type == "type_annotation"), None)
    if ann is None:
        return None
    inner = ann.named_children[0] if ann.named_children else None
    if inner is None or inner.type != "array_type":
        return None
    el = next(
        (c for c in inner.named_children if c.type in ("type_identifier", "identifier")), None
    )
    return node_text(el, source) if el is not None else None


def _entity_array_vars(root: Node, source: bytes, entities: set[str]) -> dict[str, str]:
    """Identifiers declared as ``Entity[]`` → that entity name. The endpoint of a bulk write
    (``bulk.update(accounts)``) is the SObject *element type* of the array argument, not the
    receiver, so it is resolved from the argument variable's array annotation. Only a
    locally-declared ``: Entity[]`` resolves; an inferred or unannotated array is skipped."""
    out: dict[str, str] = {}

    def go(n: Node) -> None:
        target: Node | None = None
        if n.type == "required_parameter":  # (accounts: Account[])
            target = n.child_by_field_name("pattern") or (
                n.named_children[0] if n.named_children else None
            )
        elif n.type == "variable_declarator":  # const accounts: Account[] = []
            target = n.child_by_field_name("name")
        if target is not None and target.type == "identifier":
            el = _array_element_type(n, source)
            if el is not None and el in entities:
                out[node_text(target, source)] = el
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _first_arg_identifier(call: Node, source: bytes) -> str | None:
    """Name of the first call argument when it is a plain identifier (``f(accounts)`` →
    ``accounts``), else None — inline array literals / spreads / expressions are not resolved
    (honest: the element type is not a single knowable declared symbol there)."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.named_children if c.type != "comment"), None)
    return node_text(first, source) if first is not None and first.type == "identifier" else None


def _type_arg_name(call: Node, source: bytes) -> str | None:
    """The single type argument of ``fn<T>(...)`` → ``T`` (e.g. ``query<Account>`` → Account)."""
    ta = call.child_by_field_name("type_arguments")
    if ta is None:
        return None
    inner = next(
        (c for c in ta.named_children if c.type in ("type_identifier", "identifier")), None
    )
    return node_text(inner, source) if inner is not None else None


def _detect_tsforce(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    out: list[Emit],
    repo_entities: set[str],
) -> bool:
    """ts-force outbound detection. Two read shapes + instance writes, endpoint = SObject:

    * ``RestObject.query<Account>(Account, qry)`` / ``client.query<Contact>(qry)`` → SObject
      from the generic ``<T>`` (fallback: a first-arg identifier that is a known entity).
    * ``Account.retrieve(...)`` — static call whose receiver is a known ``RestObject`` entity.
    * ``rec.insert()/update()/delete()`` — instance write. The receiver variable's type is
      resolved to a known SObject (``rec = new Account()`` or ``rec: Account``); an
      unresolvable receiver (``current.delete()`` where ``current`` is a ``Set``, or a var
      returned from a helper) is left **uncaptured** — absent beats wrong. We never tag a
      write we could not confirm is on an SObject.
    * ``bulk.update(accounts)`` — composite/bulk write where the receiver is a
      :data:`_TSFORCE_BULK_WRITERS` instance; the SObject is the element type of the array
      argument (``accounts: Account[]`` → ``Account``), resolved from the argument variable's
      declared array type. Skipped when the receiver isn't a bulk writer, the argument isn't a
      plain identifier, or its element type isn't a known SObject.

    Byte guard: the file either imports ``ts-force`` **or** references a known SObject in the
    three positions a call site uses it — as a static receiver (``Account.``), a constructed
    instance (``new Account``), or a typed binding (``: Account``) — the real call sites import
    the generated entity, not ``ts-force`` itself. The SObject set is this file's
    ``extends RestObject`` classes **plus** ``repo_entities`` (the same set resolved repo-wide),
    so a call whose entity is defined in another file still resolves. The guard is only a cheap
    pre-filter — every emit still requires the receiver to resolve to a confirmed entity, so a
    stray substring match costs a wasted AST walk, never a wrong tag. SObject is resolved from
    the code (``<T>`` / receiver / receiver-var type), else the call is skipped or ``endpoint``
    stays null — never guessed."""
    entities = _restobject_entities(record) | repo_entities
    if _TSFORCE_MARKER not in source and not any(
        e.encode() + b"." in source
        or b"new " + e.encode() in source
        or b": " + e.encode() in source
        for e in entities
    ):
        return False
    entity_vars = _entity_typed_vars(root, source, entities)
    bulk_writer_vars = set(_entity_typed_vars(root, source, _TSFORCE_BULK_WRITERS))
    array_vars = _entity_array_vars(root, source, entities)
    emitted = False

    for call in walk_calls(root):
        chain = callee_chain(call, source)
        if chain is None:
            continue
        _callee, receiver, tail = chain
        endpoint: str | None = None

        if tail in _TSFORCE_QUERY_METHODS:
            # read: SObject lives in the generic <T> or a known-entity receiver. Only accept it
            # as the endpoint when it resolves to a real entity — a bare/unbound type param
            # (``client.query<T>(qry)`` inside a generic helper) leaves endpoint null (honest;
            # the concrete SObject is known only at the caller, reachable via the call graph).
            type_arg = _type_arg_name(call, source)
            if type_arg in entities:
                endpoint = type_arg
            elif receiver in entities:
                endpoint = receiver
            # require SOME ts-force signal: a RestObject-family receiver, or a resolved SObject.
            if endpoint is None and receiver != _TSFORCE_BASE and type_arg is None:
                continue
        elif tail in _TSFORCE_WRITE_METHODS:
            # write: resolve the receiver to an SObject — a static write on the class itself
            # (``Account.delete(id)``, rare) or an instance whose variable type is a known
            # entity. An unresolvable receiver is skipped (we don't guess the instance's type).
            if receiver in entities:
                endpoint = receiver
            elif receiver in entity_vars:
                endpoint = entity_vars[receiver]
            elif receiver in bulk_writer_vars:
                # composite/bulk write: endpoint is the SObject element type of the array
                # argument (``bulk.update(accounts)`` where ``accounts: Account[]``).
                arg = _first_arg_identifier(call, source)
                endpoint = array_vars.get(arg) if arg is not None else None
                if endpoint is None:
                    continue  # unresolvable element type → skip (absent beats wrong)
            else:
                continue
        else:
            continue

        emit_outbound(call, endpoint or "", "salesforce", out, reclassify_db=True)
        emitted = True

    return emitted


def _detect(dc: DetectContext) -> Detection | None:
    out: list[Emit] = []
    heritage = getattr(dc.ctx.resolution_index, "class_heritage", None)
    if not _detect_tsforce(dc.root, dc.source, dc.path, dc.record, out, _repo_entities(heritage)):
        return None
    return Detection(out, label="salesforce")


register_detector(Detector(
    name="ts-force", gate=Gate("typescript", order=32), detect=_detect,
    frameworks=("salesforce",),
))
