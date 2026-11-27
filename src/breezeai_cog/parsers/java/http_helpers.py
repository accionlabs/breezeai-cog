"""Java HTTP calls made through the application's own helpers are recorded where they run — the
Java adapter of the shared rule (``parsers/outbound_helpers.py``; BREEZEAI-1654).

An HTTP client call is an ``api_call`` where the client is called. When the application wraps the
client in its own method, that call is made on the method's parameter and says nothing of where
the request goes; the method's callers hold the URL::

    // ApiHandler.java
    public HttpClientRequest getClient(String url, …) {               // a relay
      return requestClient(ApiUtil.METHOD_GET, url, null, headers, h);
    }
    public HttpClientRequest requestClient(String method, String url, …) {   // a helper
      HttpClientRequest request = client.request(method, apiPath + url, …);
    }
    // FileApiHandler.java (extends ApiHandler)
    String url = "id";
    HttpClientRequest apiRequest = getClient(url, null, endHandler);  // api_call GET {apiPath}id

Contract §5, "Where an outbound record is written": each call of a helper is the ``api_call``, with
the caller's URL argument in the helper's URL template and the verb the helper fixes or the
caller passes; inside the helper (and each relay) the call on its own URL parameter is a plain
statement, whether or not any of its callers resolve.

**Helper** — a method or constructor of the repository whose HTTP client request (a Vert.x 2
``HttpClient`` request, an Apache ``new HttpGet(…)`` …, matched exactly as capture matches them)
takes its URL from one of its parameters, whole or as its last part (``apiPath + url``); a method
whose URL only takes a base or an id from a parameter (``base + "/orders"``) names its endpoint
itself and keeps its record. It keeps the
URL template (``{apiPath}{url}``, the slot ``{url}``) and the verb: fixed by the request, or the
parameter that carries it (``request(method, …)``).

**Relay** — a method passing its own parameter into a helper's URL position, called on its own
class (unqualified or ``this.``, its superclasses included), on a class name, through
``super(…)`` / ``this(…)``, or on a field of its class by the field's declared type
(``this.client.put(uri, v)`` with ``HttpClient client``; a bare ``client`` only when no parameter
or local of the method has that name). It is a helper with the same template; its verb is the helper's, the
parameter it passes into the verb position, or the constant it passes there
(``ApiUtil.METHOD_GET`` folded through the repository's constants).

A method whose URL slot cannot be proven (the parameter rewritten in the method, used twice, two
parameters in the URL) is not a helper and keeps its record; one declared with a different shape
in two files is dropped (unsure).

**Call site** — a call the Java call resolver links to a helper's file under the helper's name,
with the helper's arity: the ``calls`` entry of the enclosing Function decides, so inherited and
static calls resolve as the call graph does. Two helpers the entry could mean → nothing.

**Through an interface** — a call on a field or variable typed as an interface links to the
interface's file (``HttpClient client = new HttpClientImpl(); client.put(uri, v)``). The method
is then a call site of a helper when the code shows which request runs whatever the object is:

* every concrete class of the repository implementing the interface (directly, through an
  in-repo base class, or a base's interfaces) runs, for that method, a helper — the first
  declaration up its in-repo base chain — and all those helpers make the same request (URL
  parameter, template, verb);
* the interface has at least two abstract methods (a functional interface may be implemented
  by a lambda no declaration shows), no interface of the repository extends it, it is not
  instantiated as an anonymous class anywhere (nor are its implementations), and no type
  naming it, or naming one of its implementations, in ``implements`` / ``extends`` is left
  unresolved.

Otherwise which request runs is not known, and the call is no call site.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from ..constfold import resolve_all
from ..emits import Emit
from ..index_common import record_distinct
from ..outbound_helpers import Helper, HelperPolicy, Pass, RelayCall, enclosing_function, \
    file_requests, follow_relays, link_site, site_emit, site_verb, unresolved_twin, url_slot
from ..treesitter import node_text
from .constants import collect_constants
from .imports import JavaIndex
from .scan import TYPE_DECLARATIONS, pick, superclass_of
from .scan import Names as _Names
from .scan import callee as _callee
from .scan import class_of as _class_of
from .scan import scan_classes as _scan
from .syntax import arguments, known_address, name_of, request_address

FACT = "java.http-helpers"
POLICY = HelperPolicy(
    language="java",
    relays="cross-file",
    depth=6,  # how many relays deep a helper is followed
    sticky_ambiguous=True,
    own_receivers=frozenset({"", "this", "super"}),
    reclassify=frozenset({"api_call", "db_method_call"}),
)
_VERBS = POLICY.verbs

#: A relay's callee as written: (owner candidates, exact, ``method/arity``).
Target = tuple[tuple[str, ...], bool, str]
#: A relay of a ``Class.method/arity`` key (its passes: callee argument index → own parameter
#: index, passed as it is; its literals: callee argument index → constant text).
Relay = RelayCall[str]
#: A type name as one file writes it: (fully-qualified candidates, exact) — ``_Names.candidates``.
Written = tuple[tuple[str, ...], bool]
#: A type declared in a file: (fully-qualified name, declaration kind, abstract, local (declared
#: in a method or an initializer), superclass as written or None, its interfaces as written
#: (``implements``; an interface's ``extends``), its methods as ``name/arity``: those with a
#: body, or for an interface its abstract ones).
TypeDecl = tuple[str, str, bool, bool, Written | None, tuple[Written, ...], tuple[str, ...]]
#: What one file gives the fact: (its path, helpers, relays, (class, superclass candidates,
#: exact) for each class, its type declarations, the simple names of the types it instantiates
#: as anonymous classes, each helper's request span by key — the requests that only define one).
FileFacts = tuple[str, list[tuple[str, Helper]], list[Relay],
                  list[tuple[str, tuple[str, ...], bool]], list[TypeDecl], frozenset[str],
                  list[tuple[str, tuple[int, int]]]]

#: Nodes between a type declaration and the next enclosing one that keep it a member type.
_MEMBER_PARENTS = frozenset({"program", "class_body", "interface_body", "enum_body",
                             "enum_body_declarations"})


def _signature(decl: Node, source: bytes) -> tuple[list[str], bool]:
    """(parameter names, varargs): a ``T... rest`` parameter is the last name. The positions
    before it are fixed, so a helper with varargs is still followed — called with at least as
    many arguments as it has fixed parameters (``name/k+`` in its key)."""
    params = decl.child_by_field_name("parameters")
    names: list[str] = []
    varargs = False
    for p in params.named_children if params is not None else []:
        if p.type == "formal_parameter":
            names.append(name_of(p.child_by_field_name("name"), source))
        elif p.type == "spread_parameter":
            declarator = next((c for c in p.named_children if c.type == "variable_declarator"),
                              None)
            nm = declarator.child_by_field_name("name") if declarator is not None else None
            names.append(name_of(nm, source) if nm is not None else "")
            varargs = True
    return names, varargs


def _arity(names: list[str], varargs: bool) -> str:
    return f"{len(names) - 1}+" if varargs else str(len(names))


def _members(name: str, count: int) -> Iterator[str]:
    """The keys' ``method/arity`` a call of ``name`` with ``count`` arguments may mean: the exact
    arity, then a varargs method with at most ``count`` fixed parameters."""
    yield f"{name}/{count}"
    for k in range(count, -1, -1):
        yield f"{name}/{k}+"


def _param_refs(node: Node, params: list[str], reassigned: Mapping[str, Any],
                source: bytes) -> list[str]:
    """The method's own parameters ``node`` reads (identifiers only, not rewritten in it)."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            text = node_text(n, source)
            if text in params and text not in reassigned:
                out.append(text)
        stack.extend(n.named_children)
    return out


#: Statements that end a ``case`` group: control never falls through into the next group.
_CASE_ENDS = frozenset({"break_statement", "return_statement", "throw_statement",
                        "continue_statement", "yield_statement"})


def _label_verb(label: Node, source: bytes) -> str | None:
    """The verb a ``case`` label names: a string literal (``case "GET":``) or an Apache request
    class's ``METHOD_NAME`` (``case HttpGet.METHOD_NAME:`` → ``GET``); None for anything else,
    ``default`` included."""
    expr = next(iter(label.named_children), None)
    if expr is None:
        return None
    if expr.type == "string_literal":
        frag = next((c for c in expr.named_children if c.type == "string_fragment"), None)
        value = node_text(frag, source).upper() if frag is not None else ""
        return value if value in _VERBS else None
    if expr.type == "field_access":
        obj, field = expr.child_by_field_name("object"), expr.child_by_field_name("field")
        if obj is not None and field is not None and obj.type == "identifier" \
                and node_text(field, source) == "METHOD_NAME":
            value = node_text(obj, source).removeprefix("Http").upper()
            return value if node_text(obj, source).startswith("Http") and value in _VERBS \
                else None
    return None


def _switch_verb(node: Node, verb: str, params: list[str], reassigned: Mapping[str, Any],
                 source: bytes) -> int:
    """The index of the parameter that picks ``node``'s verb through a ``switch``, or -1.

    A request with a fixed verb takes it from a parameter when it sits in a ``case`` of a
    ``switch`` on that parameter (not rewritten in the method) and every label reaching it names
    that verb::

        switch (method) {
          case HttpGet.METHOD_NAME: req = new HttpGet(uri); break;   // GET only when method is "GET"
          case "POST": case "post":  req = new HttpPost(uri); break;
        }

    Labels reaching it are its own and those of the empty groups just before it (``case A: case
    B: …``); a group before it with statements that does not end in ``break`` / ``return`` /
    ``throw`` falls through into it, so its verb is not certain."""
    group = node.parent
    while group is not None and group.type not in ("switch_block_statement_group",
                                                   "switch_rule"):
        if group.type in ("method_declaration", "constructor_declaration", "lambda_expression",
                          "class_body"):
            return -1
        group = group.parent
    if group is None:
        return -1
    block = group.parent
    switch = block.parent if block is not None else None
    cond = switch.child_by_field_name("condition") if switch is not None else None
    inner = next(iter(cond.named_children), None) if cond is not None else None
    if inner is None or inner.type != "identifier":
        return -1
    name = node_text(inner, source)
    if name not in params or name in reassigned:
        return -1
    labels = [c for c in group.named_children if c.type == "switch_label"]
    if group.type == "switch_block_statement_group":
        prev = group.prev_named_sibling
        while prev is not None and prev.type == "switch_block_statement_group":
            body = [c for c in prev.named_children if c.type != "switch_label"]
            if body:
                if body[-1].type not in _CASE_ENDS:
                    return -1  # falls through into this group with statements of its own
                break
            labels += [c for c in prev.named_children if c.type == "switch_label"]
            prev = prev.prev_named_sibling
    if not labels or any(_label_verb(lb, source) != verb.upper() for lb in labels):
        return -1
    return params.index(name)


def _requests(root: Node, source: bytes
              ) -> list[tuple[Node, str | None, Node | None, Node | None, str]]:
    """This file's HTTP client requests: (node, verb, verb argument, URI argument, framework),
    as the client packages registered for Java recognise them (``register_requests``: the
    Vert.x 2 ``HttpClient``, Apache ``HttpGet``…) — matched exactly as capture matches them."""
    return [(r.node, r.verb, r.verb_arg, r.url, r.framework or "")
            for r in file_requests("java", root, source)]


def _scan_file(root: Node, source: bytes, rel: str) -> FileFacts:
    """This file's helpers, relays and classes, and each helper's (key, request span): the
    requests that only define one."""
    names = _Names(root, source)
    found_types: list[Node] = []
    scanned = _scan(root, source, names, found_types)
    requests = _requests(root, source)
    helpers: list[tuple[str, Helper]] = []
    relays: list[Relay] = []
    classes: list[tuple[str, tuple[str, ...], bool]] = []
    spans: list[tuple[str, tuple[int, int]]] = []
    for cls in scanned:
        fields = _fields(cls.body, source)
        if cls.superclass:
            found, exact = names.candidates(cls.superclass)
            classes.append((cls.name, found, exact))
        else:
            classes.append((cls.name, (), True))
        for d in cls.decls:
            params, varargs = _signature(d.node, source)
            if not params or d.node.child_by_field_name("body") is None:
                continue
            ctor = d.node.type == "constructor_declaration"
            mname = "<init>" if ctor else name_of(d.node.child_by_field_name("name"), source)
            key = f"{cls.name}.{mname}/{_arity(params, varargs)}"
            fixed = len(params) - 1 if varargs else len(params)
            lo, hi = d.node.start_byte, d.node.end_byte
            for node, verb, verb_arg, uri, framework in requests:
                if uri is None or not lo <= node.start_byte < hi:
                    continue
                refs = _param_refs(uri, params, d.assigned, source)
                if len(refs) != 1 or params.index(refs[0]) >= fixed:
                    continue  # no parameter (an ordinary call), two, or the varargs: no slot
                template = request_address(uri, source, {}) or ""
                slot = url_slot(refs, template)
                if slot is None:
                    continue  # a base or an id from the parameter: the method names its endpoint
                vidx = -1
                if verb is None and verb_arg is not None and verb_arg.type == "identifier" \
                        and name_of(verb_arg, source) in params \
                        and name_of(verb_arg, source) not in d.assigned:
                    vidx = params.index(name_of(verb_arg, source))
                elif verb is not None:
                    vidx = _switch_verb(node, verb, params[:fixed], d.assigned, source)
                    if vidx >= 0:
                        verb = None  # the parameter picks it: each caller's value decides
                helpers.append((key, Helper(params.index(refs[0]), template, slot, verb, vidx,
                                            framework=framework or None)))
                spans.append((key, (node.start_byte, node.end_byte)))
            table: _Locals | None = None  # the method's locals: one walk, on first use
            for call in d.calls:
                args = arguments(call)
                mapping = tuple((i, params.index(name_of(a, source))) for i, a in enumerate(args)
                                if a.type == "identifier" and name_of(a, source) in params[:fixed]
                                and name_of(a, source) not in d.assigned)
                if not mapping:
                    continue
                if table is None:
                    table = _locals(d.node, source)
                callee = _callee(call, cls.name, cls.superclass, names, source) \
                    or _field_callee(call, fields, table, params, names, source) \
                    or _local_callee(call, table, params, names, source)
                if callee is None:
                    continue
                # a bare constant is the class's own (``execute(GET, uri)`` → ``Client.GET``,
                # as the constant index keys it); a parameter or local of that name is not one
                simple = cls.name.rsplit(".", 1)[-1]
                constants = tuple(
                    (i, f"{simple}.{node_text(a, source)}" if a.type == "identifier"
                     else node_text(a, source))
                    for i, a in enumerate(args)
                    if a.type in ("string_literal", "field_access", "identifier")
                    and not any(i == j for j, _ in mapping)
                    and not (a.type == "identifier" and (node_text(a, source) in params
                             or node_text(a, source) in table.declared)))
                relays.append(RelayCall(key, callee, tuple(Pass(i, p) for i, p in mapping),
                                        constants, origin=rel))
    decls, anonymous = _types(found_types, source, names)
    return rel, helpers, relays, classes, decls, anonymous, spans


def _fields(body: Node, source: bytes) -> dict[str, str | None]:
    """A class body's fields → their declared class type as written (generics dropped); None
    for a name declared twice. Primitive and array fields are left out."""
    out: dict[str, str | None] = {}
    for m in body.named_children:
        typ = m.child_by_field_name("type") if m.type == "field_declaration" else None
        if typ is None or typ.type not in ("type_identifier", "scoped_type_identifier",
                                           "generic_type"):
            continue
        written = node_text(typ, source).split("<", 1)[0].strip()
        for d in m.named_children:
            if d.type == "variable_declarator":
                record_distinct(out, name_of(d.child_by_field_name("name"), source), written)
    return out


#: Declarations whose ``name`` is a local of the method they are in.
_LOCAL_NAMES = frozenset({"variable_declarator", "formal_parameter", "catch_formal_parameter",
                          "enhanced_for_statement", "spread_parameter"})


class _Locals:
    """A method's local names, from one walk of its body (§11: never one walk per call).

    ``declared``: every name a local variable, a lambda / catch / for parameter declares.
    ``typed``: for each name declared by a local variable declaration, the class type each
    declaration gives it (``RestClient client = …``, or ``var`` with a ``new T(…)``; generics
    dropped), None where it has none; a lambda / catch / for parameter of the name adds a None."""

    __slots__ = ("declared", "typed")

    def __init__(self) -> None:
        self.declared: set[str] = set()
        self.typed: dict[str, list[str | None]] = {}

    def type_of(self, name: str) -> str | None:
        """The class type of ``name`` when the method declares it exactly once, as a typed local;
        None otherwise — not declared, declared twice, or as a lambda / catch / for parameter."""
        found = self.typed.get(name, [])
        return found[0] if len(found) == 1 else None


def _locals(decl: Node, source: bytes) -> _Locals:
    out = _Locals()
    body = decl.child_by_field_name("body")
    stack = [body] if body is not None else []
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        if n.type == "lambda_expression":
            ps = n.child_by_field_name("parameters")
            names = [node_text(ps, source)] if ps is not None and ps.type == "identifier" else \
                [node_text(c, source) for c in ps.named_children] \
                if ps is not None and ps.type == "inferred_parameters" else []
            for name in names:
                out.declared.add(name)
                out.typed.setdefault(name, []).append(None)
            continue
        if n.type not in _LOCAL_NAMES:
            continue
        nm = n.child_by_field_name("name")
        if nm is None:
            continue
        name = node_text(nm, source)
        out.declared.add(name)
        if n.type != "variable_declarator":
            out.typed.setdefault(name, []).append(None)  # a lambda / catch / for parameter
            continue
        parent = n.parent
        if parent is None or parent.type != "local_variable_declaration":
            continue  # a field of a class declared in the method: not a local of it
        typ = parent.child_by_field_name("type")
        written: str | None = None
        if typ is not None and node_text(typ, source) == "var":  # a type_identifier too
            value = n.child_by_field_name("value")
            vtype = value.child_by_field_name("type") \
                if value is not None and value.type == "object_creation_expression" \
                and value.child_by_field_name("body") is None else None
            written = node_text(vtype, source) if vtype is not None else None
        elif typ is not None and typ.type in ("type_identifier", "scoped_type_identifier",
                                              "generic_type"):
            written = node_text(typ, source)
        out.typed.setdefault(name, []).append(written.split("<", 1)[0].strip()
                                               if written else None)
    return out


def _local_callee(call: Node, table: _Locals, params: list[str], names: _Names, source: bytes
                  ) -> tuple[tuple[str, ...], bool, str] | None:
    """(owner candidates, exact, ``method/arity``) of a call on a local variable of the method
    by its declared type (``RestClient client = new RestClient(); client.request(m, uri)``), the
    same as a field's (:func:`_field_callee`); None otherwise."""
    obj = call.child_by_field_name("object")
    if call.type != "method_invocation" or obj is None or obj.type != "identifier":
        return None
    name = node_text(obj, source)
    if name in params:
        return None
    written = table.type_of(name)
    if not written:
        return None
    found, exact = names.candidates(written)
    return found, exact, f"{name_of(call.child_by_field_name('name'), source)}/" \
                         f"{len(arguments(call))}"


def _field_callee(call: Node, fields: Mapping[str, str | None], table: _Locals,
                  params: list[str], names: _Names, source: bytes
                  ) -> tuple[tuple[str, ...], bool, str] | None:
    """(owner candidates, exact, ``method/arity``) of a call on a field of the class
    (``this.client.put(…)``, or ``client.put(…)`` when no parameter or local is ``client``),
    by the field's declared type; None otherwise."""
    obj = call.child_by_field_name("object")
    if call.type != "method_invocation" or obj is None:
        return None
    if obj.type == "field_access":
        inner = obj.child_by_field_name("object")
        if inner is None or inner.type != "this":
            return None
        field = name_of(obj.child_by_field_name("field"), source)
    elif obj.type == "identifier":
        field = node_text(obj, source)
        if field in params or field not in fields or field in table.declared:
            return None
    else:
        return None
    written = fields.get(field)
    if not written:
        return None
    found, exact = names.candidates(written)
    return found, exact, f"{name_of(call.child_by_field_name('name'), source)}/" \
                         f"{len(arguments(call))}"


def _types(nodes: list[Node], source: bytes, names: _Names
           ) -> tuple[list[TypeDecl], frozenset[str]]:
    """The file's type declarations and the simple names it instantiates as anonymous classes,
    from the nodes :func:`_scan` collected (no further walk: each declaration's own members
    only)."""
    decls: list[TypeDecl] = []
    anonymous: set[str] = set()
    for n in nodes:
        if n.type == "object_creation_expression":
            typ = n.child_by_field_name("type")
            if typ is not None:
                anonymous.add(name_of(typ, source).split("<", 1)[0].rsplit(".", 1)[-1])
            continue
        chain: list[str] = []
        local = False
        cur: Node | None = n
        while cur is not None:
            if cur.type in TYPE_DECLARATIONS:
                chain.append(name_of(cur.child_by_field_name("name"), source))
            elif cur.type not in _MEMBER_PARENTS:
                local = True
            cur = cur.parent
        interface = n.type == "interface_declaration"
        modifiers = next((node_text(c, source) for c in n.children if c.type == "modifiers"), "")
        sup = superclass_of(n, source) if n.type == "class_declaration" else None
        listed = n.child_by_field_name("interfaces") or next(
            (c for c in n.named_children if c.type == "extends_interfaces"), None)
        types_list = next((c for c in listed.named_children if c.type == "type_list"), None) \
            if listed is not None else None
        interfaces = tuple(names.candidates(name_of(t, source))
                           for t in (types_list.named_children if types_list is not None else ()))
        body = n.child_by_field_name("body")
        members = list(body.named_children) if body is not None else []
        members += [m for c in members if c.type == "enum_body_declarations"
                    for m in c.named_children]
        methods: list[str] = []
        for m in members:
            if m.type != "method_declaration":
                continue
            has_body = m.child_by_field_name("body") is not None
            mods = next((node_text(c, source) for c in m.children if c.type == "modifiers"), "")
            if interface and (has_body or "static" in mods.split()):
                continue  # a default / static / private method: no implementation to look for
            if not interface and not has_body:
                continue  # an abstract method: a subclass declares it
            params, varargs = _signature(m, source)
            methods.append(f"{name_of(m.child_by_field_name('name'), source)}/"
                           f"{_arity(params, varargs)}")
        decls.append((names.qualify(*reversed(chain)), n.type, "abstract" in modifiers.split(),
                      local, names.candidates(sup) if sup else None, interfaces,
                      tuple(methods)))
    return decls, frozenset(anonymous)


#: An interface each of whose implementations is known: (its file, its abstract methods
#: ``name/arity``, for each concrete implementation its class and in-repo bases, nearest first,
#: each with the methods it declares).
Implementations = tuple[str, tuple[str, ...],
                        tuple[tuple[tuple[str, frozenset[str]], ...], ...]]


def _implementations(values: list[FileFacts], types: dict[str, Any]
                     ) -> dict[str, Implementations]:
    """The interfaces whose implementations the repository shows in full (module docstring,
    "Through an interface"), each with them. Linear in the declarations: every concrete class's
    base chain is walked once."""
    decls = [(v[0], d) for v in values for d in v[4]]
    anonymous = frozenset().union(*(v[5] for v in values))
    declared = {d[0] for _, d in decls}
    by_name: dict[str, tuple[str, TypeDecl] | None] = {}
    for rel, d in decls:
        record_distinct(by_name, d[0], (rel, d))
    unresolved: set[str] = set()  # simple names written in implements / extends, not resolved

    def resolve(written: Written) -> str | None:
        hit = pick(written[0], written[1], declared, types)
        if hit is None:
            unresolved.add(written[0][0].rsplit(".", 1)[-1])
        return hit

    supers = {d[0]: resolve(d[4]) if d[4] else None for _, d in decls}
    interfaces = {d[0]: tuple(resolve(w) for w in d[5]) for _, d in decls}
    methods = {d[0]: frozenset(d[6]) for _, d in decls}
    extended = {i for _, d in decls if d[1] == "interface_declaration"
                for i in interfaces[d[0]] if i}
    chains: dict[str, list[list[str]]] = {}
    unsure: set[str] = set()
    for _, d in decls:
        if d[1] == "interface_declaration" or d[2]:
            continue  # an interface or an abstract class is never the object itself
        chain: list[str] = []
        cur: str | None = d[0]
        while cur is not None and cur not in chain:
            if by_name.get(cur) is None:
                break  # declared twice: which one is meant is not known
            chain.append(cur)
            cur = supers.get(cur)
        else:
            for implemented in {i for c in chain for i in interfaces[c] if i}:
                chains.setdefault(implemented, []).append(chain)
            continue
        unsure.update(i for c in [*chain, d[0]] for i in interfaces.get(c, ()) if i)
    out: dict[str, Implementations] = {}
    for rel, iface in decls:
        name, kind, _, local, _, _, abstract = iface
        simple = name.rsplit(".", 1)[-1]
        found = chains.get(name)
        if kind != "interface_declaration" or local or len(abstract) < 2 or not found \
                or by_name.get(name) is None or name in extended or name in unsure \
                or simple in anonymous or simple in unresolved or any(
                    c.rsplit(".", 1)[-1] in anonymous or c.rsplit(".", 1)[-1] in unresolved
                    for ch in found for c in ch):
            continue
        out[name] = (rel, abstract, tuple(tuple((c, methods[c]) for c in ch) for ch in found))
    return out


def _agreed(impls: Mapping[str, Implementations], known: Mapping[str, Helper]
            ) -> dict[tuple[str, str], tuple[str, str]]:
    """``(interface, method/arity)`` → (the interface's file, the helper key every
    implementation runs), where all of them run a helper making the same request."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for iface, (rel, abstract, chains) in impls.items():
        for member in abstract:
            runs = {next((f"{c}.{member}" for c, ms in chain if member in ms), "")
                    for chain in chains}
            requests = {known.get(k) for k in runs}
            if "" in runs or None in requests or len(requests) != 1:
                continue  # not every implementation runs a helper, or not the same request
            out[(iface, member)] = (rel, min(runs))
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileFacts | None:
    facts = _scan_file(root, source, rel)
    return facts if any(facts[1:6]) else None


def _fold(text: str, consts: Mapping[str, str]) -> str | None:
    """A verb constant as written in a relay: a string literal or a repository constant."""
    if len(text) >= 2 and text[0] == text[-1] == '"':
        value: str | None = text[1:-1]
    else:
        value = consts.get(text)
    return value.upper() if value and value.upper() in _VERBS else None


def _resolve(values: list[FileFacts], index: Any) -> dict[str, Any] | None:
    """(helpers by key, (file, ``method``, arity) → key, key → file, file → its helpers'
    request spans). A key declared twice differently is dropped; relays are followed
    (:data:`POLICY`: across files, through the callee's superclasses when it does not declare the
    method itself, and through an interface whose implementations agree)."""
    merged: dict[str, Helper | None] = {}
    files: dict[str, str | None] = {}
    relays: list[Relay] = []
    supers_written: dict[str, tuple[tuple[str, ...], bool]] = {}
    for rel, helpers, rels, classes, _, _, _ in values:
        for key, helper in helpers:
            record_distinct(merged, key, helper)
            record_distinct(files, key, rel)
        relays.extend(rels)
        for cls, found, exact in classes:
            supers_written[cls] = (found, exact)
    types: dict[str, Any] = getattr(index, "fqcn", None) or {}
    consts: Mapping[str, str] = getattr(index, "consts", None) or {}
    known_classes = set(supers_written)
    supers = {cls: pick(found, exact, known_classes, types) if found else None
              for cls, (found, exact) in supers_written.items()}
    known = {k: v for k, v in merged.items() if v is not None}
    known_types = known_classes | {d[0] for v in values for d in v[4]}
    impls = _implementations(values, types)
    agreed = _agreed(impls, known)
    members = _member_names(known)

    def lookup_member(owner: str | None, member: str) -> Helper | None:
        name, _, count = member.partition("/")
        seen: set[str] = set()
        while owner is not None and owner not in seen:
            seen.add(owner)
            for m in _members(name, int(count)):
                hit = known.get(f"{owner}.{m}")
                if hit is None and (owner, m) in agreed:
                    hit = known.get(agreed[(owner, m)][1])  # an interface's agreed request
                if hit is not None:
                    return hit
            if name == "<init>":
                return None
            owner = supers.get(owner)
        return None

    def lookup(relay: Relay) -> Helper | None:
        owners, exact, member = relay.target
        if member.partition("/")[0] not in members:
            return None
        return lookup_member(pick(owners, exact, known_types, types), member)

    def on_round(new: dict[str, Helper], origins: dict[str, str | None]) -> None:
        nonlocal agreed, members
        agreed = _agreed(impls, known)
        members = _member_names(known)
        for k in new:
            files[k] = origins.get(k)

    follow_relays(known, relays, lookup=lookup, fold_verb=lambda text: _fold(text, consts),
                  policy=POLICY, blocked={k for k, v in merged.items() if v is None},
                  on_round=on_round)
    sites: dict[tuple[str, str], str | None] = {}
    for key in known:
        rel = files.get(key)
        if rel is None:
            continue
        member = key.rsplit(".", 1)[-1]
        name, _, arity = member.partition("/")
        if name == "<init>":
            name = _class_of(key).rsplit(".", 1)[-1]
        record_distinct(sites, (rel, f"{name}/{arity}"), key)
    for (_, member), (rel, key) in _agreed(impls, known).items():
        record_distinct(sites, (rel, member), key)
    spans = {v[0]: kept for v in values
             if (kept := frozenset(span for key, span in v[6] if key in known))}
    return {"helpers": known, "sites": sites, "files": files, "spans": spans} if known else None


def _member_names(known: Mapping[str, Helper]) -> set[str]:
    """The method names of the known helpers: a relay calling another name reaches none."""
    return {k.rsplit(".", 1)[-1].partition("/")[0] for k in known}


register_fact(FactSpec(name=FACT, language="java", gate=(b"",), collect=_collect,
                       resolve=_resolve))


def defining_requests(path: str, index: Any | None) -> frozenset[tuple[int, int]]:
    """This file's requests that only define one: a helper's request on its own URL parameter,
    by (start byte, end byte), kept in the fact when the index was built — the file is not read
    again. Each call of the helper makes the request and is the ``api_call``; the client
    packages (Vert.x 2, Apache) leave these as plain statements. (A relay's call passing the
    parameter on is skipped by the call-site detector.)"""
    fact = index_fact(index, FACT)
    if not fact:
        return frozenset()
    spans: frozenset[tuple[int, int]] = fact["spans"].get(path, frozenset())
    return spans


def _own_parameter(node: Node, source: bytes) -> bool:
    """``node`` is a parameter of the method or constructor it is written in."""
    if node.type != "identifier":
        return False
    decl = node.parent
    while decl is not None and decl.type not in ("method_declaration",
                                                 "constructor_declaration"):
        if decl.type in ("lambda_expression", "class_body"):
            return False  # a lambda's or an inner class's own name
        decl = decl.parent
    params = _signature(decl, source)[0] if decl is not None else []
    return node_text(node, source) in params


def _method_values(method: Node, source: bytes) -> dict[str, list[Node]]:
    """Every value a method gives a name — a local initializer or an assignment (``this.``
    dropped) — name → values, from one walk of the method (§11), shared by its call sites."""
    out: dict[str, list[Node]] = {}
    stack = [method]
    while stack:
        n = stack.pop()
        if n.type == "variable_declarator":
            nm, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and value is not None:
                out.setdefault(node_text(nm, source), []).append(value)
        elif n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None:
                out.setdefault(node_text(left, source).removeprefix("this."), []).append(right)
        stack.extend(n.named_children)
    return out


def _local_value(at: Node, source: bytes,
                 cache: dict[tuple[int, int], dict[str, list[Node]]]) -> Node | None:
    """The value given to ``at``'s name in the method enclosing it, when exactly one is
    (``syntax.local_value``, read from :func:`_method_values` once per method)."""
    method: Node | None = at
    while method is not None and method.type not in ("method_declaration",
                                                     "constructor_declaration"):
        method = method.parent
    if method is None:
        return None
    span = (method.start_byte, method.end_byte)
    if span not in cache:
        cache[span] = _method_values(method, source)
    found = cache[span].get(node_text(at, source), [])
    return found[0] if len(found) == 1 else None


def _site_key(sites: Mapping[tuple[str, str], str | None], name: str, count: int
              ) -> Callable[[str], str | None]:
    """``calls[].path`` → the helper a call of ``name`` with ``count`` arguments means in that
    file (its exact arity, else a varargs one), or None."""
    def key_of(path: str) -> str | None:
        return next((sites[(path, m)] for m in _members(name, count) if (path, m) in sites),
                    None)
    return key_of


def _detect(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index
    fact = index_fact(index, FACT)
    if not fact:
        return None
    helpers: dict[str, Helper] = fact["helpers"]
    sites: dict[tuple[str, str], str | None] = fact["sites"]
    names = {member.partition("/")[0] for _, member in sites}
    source = dc.source
    if not any(n.encode() in source for n in names):
        return None
    consts: dict[str, str] | None = None
    values: dict[tuple[int, int], dict[str, list[Node]]] = {}
    found: list[Emit] = []
    stack = [dc.root]
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        if n.type not in ("method_invocation", "object_creation_expression"):
            continue
        if n.type == "method_invocation":
            name = name_of(n.child_by_field_name("name"), source)
        else:
            typ = n.child_by_field_name("type")
            name = name_of(typ, source).split("<", 1)[0].rsplit(".", 1)[-1] if typ else ""
        if name not in names:
            continue
        args = arguments(n)
        fn = enclosing_function(dc.record, n.start_point[0] + 1)
        if fn is None:
            continue
        key = link_site(fn.calls, name, _site_key(sites, name, len(args)))
        if key is None:
            continue  # not a helper call, or two helpers it may be: unsure
        obj = n.child_by_field_name("object") if n.type == "method_invocation" else None
        receiver = node_text(obj, source) if obj is not None else ""
        if receiver not in POLICY.own_receivers and unresolved_twin(fn.calls, name):
            continue  # another `name` here resolves nowhere (`map.get(…)`): only an unqualified,
            # `this.` or `super.` call is surely the helper's
        helper = helpers.get(key)
        if helper is None or helper.url_index >= len(args):
            continue
        if _own_parameter(args[helper.url_index], source):
            continue  # a relay passing its own URL parameter on: its callers make the request
        if consts is None:
            cross = index.consts if isinstance(index, JavaIndex) else {}
            consts = resolve_all(collect_constants(dc.root, source), base=cross)
        arg = args[helper.url_index]
        if arg.type == "identifier":
            # `String url = "id"; getClient(url)`: a local given one constant value
            init = _local_value(arg, source, values)
            if init is not None and known_address(init, source, consts) is not None:
                arg = init
        value = request_address(arg, source, consts)
        if value is None:
            continue
        literal = known_address(args[helper.verb_index], source, consts) \
            if 0 <= helper.verb_index < len(args) else None
        found.append(site_emit(n, helper, value, site_verb(helper, literal, _VERBS), POLICY))
    return Detection(found) if found else None


register_detector(Detector(
    name="java-http-helpers",
    gate=Gate("java", order=85, guard=None, skip_fixtures=True),
    detect=_detect,
))
