"""Outbound calls made through the application's own helpers — the language-free part of the rule.

Contract §5, "Where an outbound record is written": when the application wraps an HTTP client in
its own function, the request is recorded **where that function is called**, with the caller's URL
argument in the helper's URL template; the helper's own request on its URL parameter is a plain
statement::

    getJson(path)        { new Client(BASE + path).get() }    // plain statement
    one(id)              { getJson("/orders/" + id) }          // api_call GET {BASE}/orders/{id}

Each language implements the parts that read its grammar — an **adapter** in its own package
(``parsers/<lang>/http_helpers.py``): which calls are requests, a function's parameters and
locals, how an argument renders as an address. This module holds what does not depend on the
language, written once:

* the model — :class:`Helper`, :class:`Pass`, :class:`RelayCall`;
* deciding a URL slot (:func:`url_slot`) and merging helpers declared in several places
  (:func:`merge_helpers`);
* deriving a relay's helper (:func:`derive`) and following relays to a fixed point
  (:func:`follow_relays`);
* linking a call site to a helper through the call resolver's ``calls[].path``
  (:func:`link_site`), the unresolved-name guard (:func:`unresolved_twin`), the verb a call
  site passes (:func:`site_verb`) and its record (:func:`site_emit`);
* clearing the helper's own record (:func:`clear_emits`);
* the registry through which a framework package contributes the requests it recognises
  (:func:`register_requests`), so a language adapter never imports a framework package and
  every framework stays plug and play.

Language differences that are not grammar (how far relays are followed, whether an ambiguous
relay is retried) are explicit :class:`HelperPolicy` settings, not code paths.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, Protocol, TypeVar

from tree_sitter import Node

from ..emit import statement_id
from ..schemas import FileRecord, Function, Statement
from .emits import Emit
from .index_common import record_distinct

K = TypeVar("K", bound=Hashable)

#: The verbs a request may name; a language can narrow it through :attr:`HelperPolicy.verbs`.
VERBS: frozenset[str] = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH",
                                   "TRACE", "CONNECT"})


# ── Model ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Helper:
    """A function whose request takes its URL from one of its parameters.

    Stored in a fact, so plain values only (picklable, compared field by field: two
    declarations of one key agree only when every field does)."""

    #: Position of the URL parameter as callers pass it (``self`` / an instance excluded).
    url_index: int
    #: The URL with the parameter as its slot: ``{BASE_URL}{path}``.
    template: str
    #: The part a caller's argument replaces: ``{path}``.
    slot: str
    #: The verb the request fixes, or None (unknown, or picked by :attr:`verb_index`).
    verb: str | None = None
    #: Position of the parameter that picks the verb, or -1.
    verb_index: int = -1
    #: The URL parameter's name, for a caller passing it by keyword (``url=…``); "" otherwise.
    url_name: str = ""
    #: The verb parameter's name, for a caller passing it by keyword; "" otherwise.
    verb_name: str = ""
    #: The verb parameter's literal default, used when a caller leaves it out; None otherwise.
    verb_default: str | None = None
    #: The client's framework label the helper's own request carries, moved to each call site.
    framework: str | None = None
    #: A method called through an instance (Python ``self``): a call on the class passes the
    #: instance first, which shifts every position.
    is_method: bool = False


@dataclass(frozen=True, slots=True)
class Pass:
    """One argument a relay passes on that reads one of its own parameters."""

    #: The callee's argument this fills: its position, or its keyword name.
    callee_arg: int | str
    #: The relay's own parameter it reads: position and name.
    param_index: int
    param_name: str = ""
    #: The argument rendered with the parameter as ``{name}`` (``"/api" + path`` →
    #: ``/api{path}``); None when the parameter is passed as it is and the helper's slot is kept.
    shown: str | None = None


@dataclass(frozen=True, slots=True)
class RelayCall(Generic[K]):
    """A call inside a function that passes the function's own parameters on to another one.

    ``target`` is what the language needs to find the callee (a name in the file, owner
    candidates and a member …); the engine hands it back to the adapter's lookup unread."""

    key: K
    target: Any
    passes: tuple[Pass, ...]
    #: Arguments written as constants: (callee argument, text). The adapter's ``fold_verb``
    #: reads the text (a literal already upper-cased, or a constant to fold).
    literals: tuple[tuple[int | str, str], ...] = ()
    is_method: bool = False
    #: Where the relay is declared (its file), recorded with the helper it becomes.
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class HelperPolicy:
    """A language's settings for the rule: what differs between languages without being
    grammar. Each field is a contract decision (contract §5's table reads from these)."""

    language: str
    #: How far relays are followed: not at all, within a file, or across files.
    relays: Literal["none", "same-file", "cross-file"] = "same-file"
    #: How many relays deep.
    depth: int = 4
    #: A relay key two relays derive differently is dropped for good (True) or derived again on
    #: the next round from what is then known (False).
    sticky_ambiguous: bool = False
    #: The verbs a request or a call site may name.
    verbs: frozenset[str] = VERBS
    #: Receivers that make a call surely the helper's own even when another call of the same
    #: name in the function resolves nowhere ("" is an unqualified call).
    own_receivers: frozenset[str] = frozenset({""})
    #: Semantic types a call site's record may overwrite (a known mis-tag).
    reclassify: frozenset[str] = frozenset()


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────


def url_slot(refs: Sequence[str], template: str) -> str | None:
    """The slot of a request whose URL reads ``refs`` (the function's parameters it reads,
    rewritten ones excluded) and renders as ``template``: ``{name}`` when it reads exactly one
    parameter, once, at the end of the URL. None otherwise — no parameter (an ordinary request),
    two, or one that is only a base or an id (``{base}/orders``: the function names its endpoint
    itself and keeps its record)."""
    if len(refs) != 1:
        return None
    slot = "{" + refs[0] + "}"
    if template.count(slot) != 1 or not template.endswith(slot):
        return None
    return slot


def merge_helpers(entries: Iterable[tuple[K, Helper]]) -> dict[K, Helper]:
    """Key → helper, a key declared twice with different helpers dropped (which one a call
    means is not known)."""
    merged: dict[K, Helper | None] = {}
    for key, helper in entries:
        record_distinct(merged, key, helper)
    return {k: v for k, v in merged.items() if v is not None}


def derive(helper: Helper, relay: RelayCall[Any], *, fold_verb: Callable[[str], str | None]
           ) -> Helper | None:
    """The helper ``relay`` becomes by passing its own parameter into ``helper``'s URL, or None
    when it passes none there.

    Its URL is the helper's template with the passed argument in the slot (or the helper's slot
    kept, for a parameter passed as it is). Its verb, when the helper's is picked by a parameter:
    the relay's own parameter passed there as it is (it picks it in turn); else a constant written
    there (``fold_verb``); else, when nothing is passed, the helper's default; else unknown."""
    own: dict[int | str, Pass] = {p.callee_arg: p for p in relay.passes}
    hit = own.get(helper.url_index) or own.get(helper.url_name)
    if hit is None:
        return None
    verb = helper.verb
    verb_index, verb_name = -1, ""
    if helper.verb_index >= 0:
        vhit = own.get(helper.verb_index) or own.get(helper.verb_name)
        lits = dict(relay.literals)
        lit = lits.get(helper.verb_index) or lits.get(helper.verb_name)
        if vhit is not None and (vhit.shown is None or vhit.shown == "{" + vhit.param_name + "}"):
            verb_index, verb_name = vhit.param_index, vhit.param_name  # passed on as it is
        elif lit is not None:
            verb = fold_verb(lit)
        elif vhit is None:
            verb = helper.verb_default.upper() if helper.verb_default else None
        else:
            verb = None  # an expression: unknown
    if hit.shown is None:
        template, slot = helper.template, helper.slot
    else:
        template, slot = helper.template.replace(helper.slot, hit.shown), "{" + hit.param_name + "}"
    return replace(helper, url_index=hit.param_index, url_name=hit.param_name, template=template,
                   slot=slot, verb=verb, verb_index=verb_index, verb_name=verb_name,
                   verb_default=None, is_method=relay.is_method)


def follow_relays(
    known: dict[K, Helper],
    relays: Sequence[RelayCall[K]],
    *,
    lookup: Callable[[RelayCall[K]], Helper | None],
    fold_verb: Callable[[str], str | None],
    policy: HelperPolicy,
    blocked: set[K] | None = None,
    on_round: Callable[[dict[K, Helper], dict[K, str | None]], None] | None = None,
) -> dict[K, Helper]:
    """Add to ``known`` every relay that reaches a helper, round by round up to
    ``policy.depth``: a relay of a relay is found on the next round. ``lookup`` finds the helper
    a relay calls from what is known so far; ``blocked`` keys are never derived (a name declared
    twice, a helper declared ambiguously). ``on_round(new, origins)`` runs after each round that
    added something (a language recomputing what depends on ``known``). Returns ``known``."""
    blocked = set() if blocked is None else blocked
    if policy.relays == "none":
        return known
    for _ in range(policy.depth):
        derived: dict[K, Helper | None] = {}
        origins: dict[K, str | None] = {}
        for relay in relays:
            if relay.key in known or relay.key in blocked:
                continue
            helper = lookup(relay)
            if helper is None:
                continue
            new = derive(helper, relay, fold_verb=fold_verb)
            if new is None:
                continue
            record_distinct(derived, relay.key, new)
            record_distinct(origins, relay.key, relay.origin)
        added = {k: v for k, v in derived.items() if v is not None}
        if policy.sticky_ambiguous:
            blocked.update(k for k, v in derived.items() if v is None)
        if not added:
            break
        known.update(added)
        if on_round is not None:
            on_round(added, origins)
    return known


def verb_literal(text: str | None, verbs: frozenset[str]) -> str | None:
    """A literal as a verb: upper-cased when it names one, else None."""
    return text.upper() if text and text.upper() in verbs else None


# ── Call sites ───────────────────────────────────────────────────────────────────────────────


def enclosing_function(record: FileRecord, line: int) -> Function | None:
    """The innermost function of ``record`` spanning ``line``."""
    best: Function | None = None
    for fn in record.functions:
        start, end = fn.startLine or 0, fn.endLine or 0
        if start <= line <= end and (
                best is None or end - start < (best.endLine or 0) - (best.startLine or 0)):
            best = fn
    return best


def link_site(calls: Iterable[Any], name: str, key_of: Callable[[str], K | None]) -> K | None:
    """The one helper a call of ``name`` means: the call resolver's ``calls`` entries of that
    name with a file (``calls[].path``), each turned into a helper key by ``key_of`` (None: not a
    helper there). None when no entry, or two different helpers, match."""
    keys: set[K] = set()
    for c in calls:
        if c.name == name and c.path:
            key = key_of(c.path)
            if key is not None:
                keys.add(key)
    return next(iter(keys)) if len(keys) == 1 else None


def unresolved_twin(calls: Iterable[Any], name: str) -> bool:
    """Another call of ``name`` in the function resolves nowhere (``map.get(…)`` beside a helper
    ``get``). The resolver's ``calls`` are per name and file, not per call, so a qualified call of
    that name is then not surely the helper's."""
    return any(c.name == name and not c.path for c in calls)


def site_verb(helper: Helper, literal: str | None, verbs: frozenset[str]) -> str | None:
    """The verb of a call of ``helper``: the helper's own, or — when a parameter picks it — the
    ``literal`` the call passes there (or the parameter's default the adapter substituted)."""
    if helper.verb_index < 0:
        return helper.verb
    return verb_literal(literal, verbs)


def site_emit(node: Node, helper: Helper, value: str, verb: str | None,
              policy: HelperPolicy) -> Emit:
    """The ``api_call`` record of a call of ``helper`` whose URL argument renders as ``value``."""
    fields: dict[str, Any] = {"semanticType": "api_call", "method": verb,
                              "endpoint": helper.template.replace(helper.slot, value)}
    if helper.framework:
        fields["framework"] = helper.framework  # the helper's client: its label moves too
    return Emit(node, fields, enrich="unclassified", reclassify=policy.reclassify)


# ── The helper's own request ─────────────────────────────────────────────────────────────────

#: A helper's own request becomes a plain statement: every field the request detectors set.
CLEARED: Mapping[str, None] = {"semanticType": None, "method": None, "endpoint": None,
                               "framework": None}


def statement_node(node: Node, path: str, statements: Mapping[str, Statement]) -> Node | None:
    """The captured statement holding ``node``: ``node`` or the nearest ancestor a statement id
    names, with the same node type."""
    cur: Node | None = node
    while cur is not None:
        stmt = statements.get(statement_id(path, cur.start_point[0] + 1, cur.start_point[1]))
        if stmt is not None and stmt.nodeType == cur.type:
            return cur
        cur = cur.parent
    return None


def clear_emits(requests: Iterable[Node], path: str, record: FileRecord) -> list[Emit]:
    """Emits turning each helper's own request (its statement, if captured as an ``api_call``)
    into a plain statement."""
    ids = {s.id: s for s in record.statements if s.semanticType == "api_call"}
    out: list[Emit] = []
    for request in requests:
        node = statement_node(request, path, ids)
        if node is not None:
            out.append(Emit(node, dict(CLEARED), enrich="same"))
    return out


# ── Requests contributed by framework packages ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Request:
    """An outbound request a recognizer found in a file."""

    #: The request: the node a capture record is written for.
    node: Node
    #: The expression its URL comes from, or None (not read).
    url: Node | None
    #: The verb it fixes, or None.
    verb: str | None = None
    #: The argument carrying its verb (``request(method, …)``), or None.
    verb_arg: Node | None = None
    #: The framework label its record carries, or None.
    framework: str | None = None


class Recognizer(Protocol):
    """``(root, source) → requests``: every request of one client kind in the file."""

    def __call__(self, root: Node, source: bytes) -> Iterable[Request]: ...


@dataclass(frozen=True, slots=True)
class RequestSource:
    """A framework package's contribution: what it recognises, for which language, and the
    bytes a file needs before it is asked."""

    name: str
    language: str
    gate: tuple[bytes, ...]
    recognize: Recognizer = field(compare=False)


_SOURCES: dict[str, list[RequestSource]] = {}
_lock = threading.Lock()


def register_requests(source: RequestSource) -> RequestSource:
    """Register a framework's request recognizer for its language (idempotent by name). The
    module calling this is imported by detector discovery, in every process."""
    with _lock:
        existing = _SOURCES.setdefault(source.language, [])
        for other in existing:
            if other.name == source.name:
                return other
        existing.append(source)
        existing.sort(key=lambda s: s.name)
    return source


def request_sources(language: str) -> list[RequestSource]:
    """The recognizers registered for ``language``, by name."""
    from .additive import discover_detectors

    discover_detectors()
    return list(_SOURCES.get(language, ()))


def file_requests(language: str, root: Node, source: bytes) -> list[Request]:
    """Every request the registered recognizers of ``language`` find in the file, in the order
    the recognizers are registered (by name), each recognizer only on a file its gate admits."""
    out: list[Request] = []
    for src in request_sources(language):
        if any(g in source for g in src.gate):
            out.extend(src.recognize(root, source))
    return out


# ── Framework labels a request carries ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RequestLabel:
    """A framework package's evidence that a request is made through its client: the label a
    helper's own request carries (``axios``), which moves to each of its call sites."""

    name: str
    language: str
    #: Bytes a file needs before the evidence is read; empty: every file.
    gate: tuple[bytes, ...]
    #: ``(root, source) → (call → made through this client?)``, or None when the file has none.
    evidence: Callable[[Node, bytes], Callable[[Node], bool] | None] = field(compare=False)


_LABELS: dict[str, list[RequestLabel]] = {}


def register_label(label: RequestLabel) -> RequestLabel:
    """Register a framework's request label for its language (idempotent by name)."""
    with _lock:
        existing = _LABELS.setdefault(label.language, [])
        for other in existing:
            if other.name == label.name:
                return other
        existing.append(label)
        existing.sort(key=lambda lb: lb.name)
    return label


def request_labels(language: str) -> list[RequestLabel]:
    """The labels registered for ``language``, by name."""
    from .additive import discover_detectors

    discover_detectors()
    return list(_LABELS.get(language, ()))


class FileLabels:
    """The registered labels' evidence for one file, read once on first use."""

    __slots__ = ("_language", "_root", "_source", "_read")

    def __init__(self, language: str, root: Node, source: bytes) -> None:
        self._language, self._root, self._source = language, root, source
        self._read: list[tuple[str, Callable[[Node], bool]]] | None = None

    def label_of(self, call: Node) -> str | None:
        """The one label whose evidence vouches for ``call``; None when none, or several, do."""
        if self._read is None:
            self._read = []
            for lb in request_labels(self._language):
                if lb.gate and not any(g in self._source for g in lb.gate):
                    continue
                evidenced = lb.evidence(self._root, self._source)
                if evidenced is not None:
                    self._read.append((lb.name, evidenced))
        hits = [name for name, evidenced in self._read if evidenced(call)]
        return hits[0] if len(hits) == 1 else None
