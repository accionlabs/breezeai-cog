"""Shared statement-record emission for --capture-statements (all languages).

The per-language ``statements.py`` yields statement nodes (``_iter_in_scope``) and
supplies a language-specific ``name_of`` + ``call_details`` + call node type; this
module turns one statement node into its ``Statement`` record(s):

  * a **base** structural record (``nodeType`` = the AST node), carrying the *first*
    api/db/query hit found in the statement's own expression — backward-compatible with
    the single-classification model, and
  * one **synthetic** same-span record per *additional* hit (``nodeType`` = the call
    node type, following the annotation-route precedent) so method chains and
    multi-call expressions don't lose every hit after the first.

Calls are gathered by ``_iter_calls``, which walks the statement's own expression but
**stops at nested EMIT_TYPES statements** (those are emitted and classified on their
own — this is also what keeps a call nested in an ``if``/``for``/``try`` *body* from
being mis-attributed to the control statement). It does *not* stop at scopes, so a
concise-bodied lambda (``x => repo.save(x)``) is still classified here.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Callable, Collection, Iterator

from tree_sitter import Node

from ..emit import disambiguate, statement_id
from ..schemas import Decorator, Statement
from .detection import classify_call, text_has_query
from .treesitter import first_line, node_text

# Per-file collector for concatenations skipped by the fold cap. ``render_concat`` records
# the line of each; the executor resets it around each file and emits ONE human-readable
# summary — far cleaner than a machine log line per concat shredding the progress display.
_skipped_concat_lines: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "skipped_concat_lines", default=None
)

# Per-file set of names known to be HTTP clients that a callee-substring hint can't reach —
# a wrapped axios instance or a config-object wrapper call (see collect_http_client_ids). A
# parser sets it (and resets it) around a file so classify_statement can pass it to the shared
# api-call classifier without threading the value through every build_function call. Defaults
# empty, so a language/file that never sets it — and any parser that doesn't opt in — is
# unaffected. The setting parser MUST reset in a finally so it never leaks to the next file
# handled by the same (possibly reused) worker process.
_http_client_ids: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "http_client_ids", default=frozenset()
)


def set_http_client_ids(ids: frozenset[str]) -> contextvars.Token[frozenset[str]]:
    """Set the per-file HTTP-client name set; returns a token to pass to
    :func:`reset_http_client_ids` (in a ``finally``)."""
    return _http_client_ids.set(ids)


def reset_http_client_ids(token: contextvars.Token[frozenset[str]]) -> None:
    _http_client_ids.reset(token)


def begin_concat_tracking() -> None:
    """Start collecting skipped-concat lines for the current file (call before parsing)."""
    _skipped_concat_lines.set([])


def summarize_skipped_concats(path: str) -> str | None:
    """One human-readable summary line for the concats skipped since
    :func:`begin_concat_tracking` (and clear the collector), or ``None`` if none."""
    lines = _skipped_concat_lines.get() or []
    _skipped_concat_lines.set(None)
    if not lines:
        return None
    shown = ", ".join(str(n) for n in lines[:10])
    more = "" if len(lines) <= 10 else f" (+{len(lines) - 10} more)"
    plural = "s" if len(lines) != 1 else ""
    return (
        f"{path}: skipped endpoint resolution for {len(lines)} deeply-nested string "
        f"concatenation{plural} (over {_MAX_CONCAT_DEPTH} levels — typically generated "
        f"HTML/JS builders); the statement{plural} {'are' if plural else 'is'} still "
        f"captured, only the endpoint is omitted. Line{plural}: {shown}{more}"
    )


# (callee, method, first_string_arg) or None for a single call node.
CallDetails = Callable[[Node, bytes], "tuple[str, str, str | None] | None"]
NameOf = Callable[[Node, bytes], "str | None"]


def _trailing_comment_end(node: Node, source: bytes) -> int:
    """``end_byte`` extended over an inline (trailing) comment that immediately follows
    ``node`` on the same line, else ``node.end_byte``. This folds ``x = 5; // note`` into the
    statement's own ``text`` (the comment is a sibling *after* the node's bytes, so a plain
    ``node_text`` would drop it). Language-agnostic: every grammar's comment node type ends in
    ``comment`` (``comment`` / ``line_comment`` / ``block_comment`` / ``multiline_comment`` /
    ``groovydoc_comment`` …).

    Steps over a lone same-line separator sibling (a bare ``;`` / ``,`` the grammar breaks out
    as its own node — e.g. Java's ``enum_body_declarations`` after the last enum constant) so a
    trailing doc on the *last* member still folds. A separator is recognised only when its whole
    text is punctuation, so a real following statement (``a = 1; b = 2; // c``) is never skipped
    (its own fold claims the comment)."""
    sib = node.next_named_sibling
    if (
        sib is not None
        and not sib.type.endswith("comment")
        and sib.start_point[0] == node.end_point[0]
        and node_text(sib, source).strip() in (";", ",", "")
    ):
        sib = sib.next_named_sibling
    if sib is not None and sib.type.endswith("comment") and sib.start_point[0] == node.end_point[0]:
        return sib.end_byte
    return node.end_byte


def text_with_trailing_comment(node: Node, source: bytes) -> str:
    """``node``'s source text, extended to include a same-line trailing comment (see
    :func:`_trailing_comment_end`)."""
    return source[node.start_byte : _trailing_comment_end(node, source)].decode("utf-8", "replace")


# --- Endpoint resolution, shared across languages -------------------
# HTTP verbs that may appear as the *first argument* (``request('GET', url)``).
HTTP_VERB_ARGS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
UrlRenderer = Callable[[Node, bytes], "str | None"]


def url_placeholder(expr_text: str) -> str:
    """A non-string URL sub-expression -> ``{name}`` (simple name) or ``{param}``."""
    simple = expr_text.rsplit(".", 1)[-1].strip()
    return "{" + simple + "}" if simple and simple.replace("_", "").isalnum() else "{param}"


def strip_leading_base(url: str) -> str:
    """Drop a leading interpolated base/host segment so the path matches inbound routes:
    ``{base}/users/{id}`` -> ``/users/{id}`` (a leading ``{...}`` with no ``/`` is kept)."""
    if url.startswith("{"):
        slash = url.find("/")
        if slash != -1:
            return url[slash:]
    return url


def normalize_route_endpoints(routes: list[Statement]) -> list[Statement]:
    """Normalize emitted route endpoints to one leading slash and no trailing slash."""
    for route in routes:
        if route.semanticType != "route" or route.endpoint is None:
            continue
        endpoint = f"/{route.endpoint.lstrip('/')}".rstrip("/")
        route.endpoint = endpoint or "/"
    return routes


#: Max binary-expression nesting ``render_concat`` will fold. String concatenation is
#: left-associative, so ``a + b + … + z`` is a deeply *nested* tree; folding it recurses
#: once per ``+`` (``render_concat`` ↔ the language ``_render_url``). Generated HTML/JS
#: builders chain hundreds-to-thousands of ``+`` in one statement (800+ levels observed),
#: which overflows the Python stack (``RecursionError``). A real URL/path concat
#: is <10 parts, so bailing to ``None`` past this bound loses nothing meaningful — and 100 ×
#: ~2 frames/level stays well under the 1000-frame limit even atop the parser's own stack.
_MAX_CONCAT_DEPTH = 100


def set_concat_depth(depth: int | None) -> None:
    """Override the fold cap for this process (from ``Settings.max_concat_depth`` /
    ``BREEZEAI_COG_MAX_CONCAT_DEPTH`` / ``--max-concat-depth``). The executor calls this per
    file so it applies in every worker process. No-op for a falsy/non-positive value."""
    global _MAX_CONCAT_DEPTH
    if depth and depth > 0:
        _MAX_CONCAT_DEPTH = depth


def _nests_too_deep(node: Node, limit: int) -> bool:
    """True if ``node``'s binary-expression nesting exceeds ``limit``. Measured with an
    explicit stack (the deep tree is exactly why we can't recurse) and early-exits at
    ``limit`` + 1, so a pathological concat costs O(limit), not O(nodes)."""
    stack = [(node, 0)]
    while stack:
        n, depth = stack.pop()
        if depth > limit:
            return True
        if n.type == "binary_expression":
            for c in n.named_children:
                stack.append((c, depth + 1))
    return False


def render_concat(node: Node, source: bytes, render: UrlRenderer) -> str | None:
    """Render a binary string-concatenation node to a path (non-string parts -> ``{name}``).
    Pathologically deep concatenations (generated HTML builders) are not a real endpoint and
    would overflow the stack, so they return ``None`` past :data:`_MAX_CONCAT_DEPTH`."""
    if _nests_too_deep(node, _MAX_CONCAT_DEPTH):
        # Record the line for the executor's per-file summary (see summarize_skipped_concats).
        tracker = _skipped_concat_lines.get()
        if tracker is not None:
            tracker.append(node.start_point[0] + 1)
        return None
    rendered = [render(c, source) for c in node.named_children]
    if not any(r is not None for r in rendered):
        return None
    return "".join(
        r if r is not None else url_placeholder(node_text(c, source))
        for r, c in zip(rendered, node.named_children)
    )


def resolve_endpoint(
    arg_nodes: list[Node], source: bytes, render: UrlRenderer
) -> tuple[str | None, str | None]:
    """(endpoint, override_method) for positional call args, given a language ``render``.
    Handles the verb-first form ``request('GET', url)`` — the verb is the method, the URL
    is the next argument; otherwise resolves the first argument."""
    if not arg_nodes:
        return None, None
    first = arg_nodes[0]
    if len(arg_nodes) >= 2:
        verb = render(first, source)
        if verb and verb.lower() in HTTP_VERB_ARGS:
            return render(arg_nodes[1], source), verb.lower()
    return render(first, source), None


def _extract_verbs(methods_node: Node | None, source: bytes | None = None) -> list[str]:
    """Extract HTTP verb strings from a route methods argument (e.g. ``['GET', 'POST']`` or ``'GET'``).

    Handles ``array_creation_expression -> array_element_initializer -> string`` as well
    as plain single-string cases. Returns a list of uppercased verbs.
    """
    if methods_node is None:
        return []

    curr = methods_node
    if curr.type == "argument" and curr.named_children:
        curr = curr.named_children[0]

    def _get_string(node: Node) -> str | None:
        if node.type == "string":
            frag = next((c for c in node.named_children if c.type == "string_content"), None)
            if frag is not None:
                return (
                    node_text(frag, source)
                    if source is not None
                    else frag.text.decode("utf-8", "replace")
                )
            text = (
                node_text(node, source)
                if source is not None
                else node.text.decode("utf-8", "replace")
            )
            return text.strip("'\"")
        if node.type == "encapsed_string":
            text = (
                node_text(node, source)
                if source is not None
                else node.text.decode("utf-8", "replace")
            )
            return text.strip("'\"")
        return None

    # Plain single-string case: 'GET', "POST"
    single = _get_string(curr)
    if single is not None:
        v = single.strip()
        return [v.upper()] if v else []

    verbs: list[str] = []
    # Array case: ['GET', 'POST'], array('GET', 'POST')
    if curr.type == "array_creation_expression":
        for elem in curr.named_children:
            target = elem
            if elem.type == "array_element_initializer":
                val_node = elem.child_by_field_name("value")
                target = (
                    val_node
                    if val_node is not None
                    else (elem.named_children[-1] if elem.named_children else elem)
                )
            s = _get_string(target)
            if s and s.strip():
                verbs.append(s.strip().upper())
        return verbs

    # General fallback for any other wrapping structure
    for c in curr.named_children:
        target = c
        if c.type == "array_element_initializer":
            val_node = c.child_by_field_name("value")
            target = (
                val_node
                if val_node is not None
                else (c.named_children[-1] if c.named_children else c)
            )
        s = _get_string(target)
        if s and s.strip():
            verbs.append(s.strip().upper())

    return verbs


_HANDLER_STRING_RE = re.compile(r"^[\w\\]+@\w+$")
_METHOD_RE = re.compile(r"^\w+$")


def _resolve_handler(arg_node: Node | None, source: bytes | None = None) -> str | None:
    r"""Resolve a route handler argument into a canonical 'Controller@method' string or None.

    - If arg_node is an array_creation_expression matching [Controller::class, 'method'],
      resolves to 'Controller@method'.
    - If arg_node is a string matching ^[\w\\]+@\w+$ (e.g. 'WebhookController@handle'),
      keeps it as-is.
    - For anything else (closures, variables, other expressions), returns None.
    """
    if arg_node is None:
        return None

    curr = arg_node
    if curr.type == "argument" and curr.named_children:
        curr = curr.named_children[0]

    def _get_string(node: Node) -> str | None:
        if node.type == "string":
            frag = next((c for c in node.named_children if c.type == "string_content"), None)
            if frag is not None:
                return (
                    node_text(frag, source)
                    if source is not None
                    else frag.text.decode("utf-8", "replace")
                )
            text = (
                node_text(node, source)
                if source is not None
                else node.text.decode("utf-8", "replace")
            )
            return text.strip("'\"")
        if node.type == "encapsed_string":
            text = (
                node_text(node, source)
                if source is not None
                else node.text.decode("utf-8", "replace")
            )
            return text.strip("'\"")
        return None

    # String matching ^[\w\\]+@\w+$
    s = _get_string(curr)
    if s is not None:
        if _HANDLER_STRING_RE.match(s):
            return s
        return None

    # Array matching [Controller::class, 'method']
    if curr.type == "array_creation_expression":
        elems = curr.named_children
        if len(elems) == 2:

            def _unwrap_elem(e: Node) -> Node:
                if e.type == "array_element_initializer":
                    val = e.child_by_field_name("value")
                    return (
                        val
                        if val is not None
                        else (e.named_children[-1] if e.named_children else e)
                    )
                return e

            e0 = _unwrap_elem(elems[0])
            e1 = _unwrap_elem(elems[1])

            if e0.type == "class_constant_access_expression":
                e0_text = (
                    node_text(e0, source)
                    if source is not None
                    else e0.text.decode("utf-8", "replace")
                )
                if "::" in e0_text:
                    lhs, rhs = e0_text.rsplit("::", 1)
                    if rhs.strip().lower() == "class":
                        class_name = lhs.strip()
                        method_name = _get_string(e1)
                        if method_name and _METHOD_RE.match(method_name):
                            return f"{class_name}@{method_name}"

    return None


def _is_call_node(node_type: str, call_type: str | Collection[str]) -> bool:
    return node_type == call_type if isinstance(call_type, str) else node_type in call_type


def _iter_calls(
    node: Node,
    emit_types: Collection[str],
    call_type: str | Collection[str],
    stmt_expr: Collection[str],
    containers: Collection[str],
    call_barrier_types: Collection[str] = (),
) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in call_barrier_types:
            continue
        if child.type in emit_types:
            continue  # a nested statement — classified on its own
        if child.type in stmt_expr and node.type in containers:
            continue  # a bare statement-position expression (its own statement — Python)
        if _is_call_node(child.type, call_type):
            yield child
        yield from _iter_calls(
            child, emit_types, call_type, stmt_expr, containers, call_barrier_types
        )


def _calls_in_statement(
    node: Node,
    emit_types: Collection[str],
    call_type: str | Collection[str],
    stmt_expr: Collection[str],
    containers: Collection[str],
    call_barrier_types: Collection[str] = (),
) -> Iterator[Node]:
    # The statement node may itself be a call — a bare Python call-statement
    # (``session.add(x)``) has no expression-statement wrapper.
    if _is_call_node(node.type, call_type):
        yield node
    yield from _iter_calls(node, emit_types, call_type, stmt_expr, containers, call_barrier_types)


#: Block/body node types a control-flow statement wraps (empirically verified across
#: TS/JS, Python, Java, C#, C++, Groovy, Kotlin). Used to take a control statement's **header**
#: as the text up to its first such child — so a multi-line condition is kept whole while the
#: body (already captured as its own child statements) is not duplicated into the header.
_BODY_BLOCK_TYPES = frozenset(
    {
        "statement_block",
        "block",
        "compound_statement",
        "switch_body",
        "control_structure_body",
    }
)


def _control_flow_header(node: Node, source: bytes) -> str:
    """A control-flow statement's header (its condition/clause). When the condition spans
    multiple lines, keep it whole up to the body block (so it isn't truncated to the first
    line); the body stays in the child statements, never duplicated here. Single-line headers —
    and constructs with no distinct block body (a braceless body, or a grammar like VB) — keep
    the first physical line unchanged, which also preserves a same-line trailing comment."""
    body = next((c for c in node.named_children if c.type in _BODY_BLOCK_TYPES), None)
    if body is not None and body.start_point[0] > node.start_point[0]:  # multi-line header only
        return source[node.start_byte : body.start_byte].decode("utf-8", "replace").rstrip()
    return first_line(node_text(node, source))


def classify_statement(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    limit: int,
    seen_ids: set[str],
    emit_types: Collection[str],
    control_flow: Collection[str],
    call_type: str | Collection[str],
    name_of: NameOf,
    call_details: CallDetails,
    stmt_expr: Collection[str] = (),
    container_types: Collection[str] = (),
    language: str | None = None,
    typed_db_ids: frozenset[str] | None = None,
    decorators: list[Decorator] | None = None,
    call_barrier_types: Collection[str] = (),
) -> list[Statement]:
    # ``code_text`` (comment-free) drives query/semantic detection; ``display_text`` is what
    # lands on the record — for a normal statement it folds in a same-line trailing comment
    # (``x = 5; // note``); a control-flow statement keeps only its header line (which already
    # carries any comment on that line), so no separate fold is needed.
    code_text = node_text(node, source)
    if node.type in control_flow:
        code_text = display_text = _control_flow_header(node, source)
    else:
        display_text = text_with_trailing_comment(node, source)
    start, col = node.start_point[0] + 1, node.start_point[1]
    end = node.end_point[0] + 1

    # All api/db/query hits in this statement's own expression, deduped by (kind, method).
    hits: list[tuple[str, str, str | None, str | None, Node]] = []
    seen_hit: set[tuple[str, str]] = set()
    for call in _calls_in_statement(
        node, emit_types, call_type, stmt_expr, container_types, call_barrier_types
    ):
        det = call_details(call, source)
        if det is None:
            continue
        classified = classify_call(
            det[0],
            det[1],
            det[2],
            language,
            typed_db_ids=typed_db_ids,
            http_client_ids=_http_client_ids.get() or None,
        )
        if classified is None:
            continue
        sem, meth, dh = classified
        key = (sem, meth)
        if key in seen_hit:
            continue
        seen_hit.add(key)
        ep = det[2] if sem == "api_call" else None
        hits.append((sem, meth, ep, dh, call))

    records: list[Statement] = []
    if hits:
        semantic, method_value, endpoint, hint, _ = hits[0]
    else:
        semantic = method_value = endpoint = hint = None
        if text_has_query(code_text):  # raw SQL/Cypher string literal, no classified call
            semantic = "query_statement"
    records.append(
        Statement(
            id=disambiguate(statement_id(path, start, col), seen_ids),
            parentId=parent_id,
            nodeType=node.type,
            semanticType=semantic,
            text=display_text,
            name=name_of(node, source),
            method=method_value,
            endpoint=endpoint,
            dataAccessHint=hint,
            decorators=decorators or [],
            startLine=start,
            endLine=end,
            path=path,
        )
    )
    # One synthetic record per additional hit, at the same span (fields stay single-valued).
    for semantic, method_value, endpoint, hint, call in hits[1:]:
        cs, ccol = call.start_point[0] + 1, call.start_point[1]
        records.append(
            Statement(
                id=disambiguate(statement_id(path, cs, ccol), seen_ids),
                parentId=parent_id,
                nodeType=call.type,
                semanticType=semantic,
                text=node_text(call, source),
                method=method_value,
                endpoint=endpoint,
                dataAccessHint=hint,
                startLine=start,
                endLine=end,
                path=path,
            )
        )
    return records


# --- Member declarations (enum members + other name/text-only declarations) --------------
# Enum members — and declarations that carry no executable expression — are captured as flat
# Statements parented to their owner (the enum Class), so their source ``text`` is queryable,
# using the same two-axis model as every other statement (``semanticType`` stays null: they
# are structural, not route/db/api/event). The declared name goes on ``name``. This replaces
# the older ``Class.metadata["constants"]`` channel — a member's value, when the language has
# one, stays inside ``text`` (``ACTIVE("A")`` / ``Admin = 'admin'`` / ``Active = 1``).

_MEMBER_NAME_TYPES = ("identifier", "simple_identifier", "property_identifier")


def _member_name(node: Node, source: bytes) -> str | None:
    """The declared name of a member node: the ``name`` field if the grammar exposes one,
    else the first identifier-like child, else the node's own text when it *is* a bare
    identifier (a valueless TS ``property_identifier`` enum member). Honest-null otherwise."""
    nm = node.child_by_field_name("name")
    if nm is not None:
        return node_text(nm, source)
    first = next((c for c in node.named_children if c.type in _MEMBER_NAME_TYPES), None)
    if first is not None:
        return node_text(first, source)
    if node.type in _MEMBER_NAME_TYPES:
        return node_text(node, source)
    return None


def member_statement(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    limit: int,
    seen_ids: set[str],
    name: str | None = None,
    decorators: list[Decorator] | None = None,
    semantic_type: str | None = None,
) -> Statement:
    """One flat declaration Statement for ``node`` (``nodeType`` = the AST node, ``text`` =
    its source). ``name`` defaults to the member's declared name; ``semantic_type`` marks the
    member's role on the same record while ``nodeType`` stays the raw grammar type."""
    start, col = node.start_point[0] + 1, node.start_point[1]
    end = node.end_point[0] + 1
    return Statement(
        id=disambiguate(statement_id(path, start, col), seen_ids),
        parentId=parent_id,
        nodeType=node.type,
        semanticType=semantic_type,
        # Fold a same-line trailing doc (``kCAPLC09 = 1000, ///< Apply Status``) into the
        # member's text — the high-value constant-doc case.
        text=text_with_trailing_comment(node, source),
        name=name if name is not None else _member_name(node, source),
        decorators=decorators or [],
        startLine=start,
        endLine=end,
        path=path,
    )


def emit_enum_members(
    body: Node,
    source: bytes,
    path: str,
    *,
    member_types: Collection[str],
    parent_id: str,
    limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    """Emit one Statement per enum member — a direct child of ``body`` whose type is in
    ``member_types`` — parented to the enum's Class id. Each carries ``semanticType`` =
    ``enum_member`` (the cross-language role) while ``nodeType`` keeps its raw grammar type."""
    return [
        member_statement(
            node,
            source,
            path,
            parent_id=parent_id,
            limit=limit,
            seen_ids=seen_ids,
            semantic_type="enum_member",
        )
        for node in body.named_children
        if node.type in member_types
    ]
