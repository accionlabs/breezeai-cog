"""jQuery AJAX ``api_call`` detection for JavaScript/TypeScript (and inline ``<script>`` blocks).

jQuery's request helpers (``$.ajax``/``$.get``/``$.post``/``$.getJSON``/``$.getScript`` and
``$(sel).load(url)``) are not HTTP-client calls the shared ``api_calls.match_api`` can see — the
receiver is ``$``/``jQuery``, which is not (and must not become) a client hint: ``$`` is far too
ambiguous. This detector recovers them from evidence instead, reusing the ``api_call`` semantic
type and the shared ``_emit_outbound`` (``framework="jquery"``, real HTTP ``method``, ``endpoint``
= the URL literal).

**Recognition is conservative** — a call is jQuery only when all of these hold:

* the receiver is ``$`` or ``jQuery`` and the file does not bind that name to something else
  (``const $ = customLibrary``, an unrelated ``$`` parameter or import → the file's ``$`` is
  untrusted, so nothing is emitted for it);
* a ``$`` receiver additionally needs *evidence* that jQuery is present: an import/``require`` of
  ``jquery``, an alias (``const $ = jQuery``), or a ``jquery*.js`` script reference. The name
  ``jQuery`` is distinctive enough to stand on its own;
* the call shape matches (known method; for ``.load`` the receiver must be a ``$(…)``/``jQuery(…)``
  selection).

**Options-object exception:** ``$.ajax({ url: "<literal>", … })`` is recognised without evidence —
a literal ``url`` in a settings object is a deliberate, narrow signal. It never applies to
``get``/``post``/…, to ``.load``, or to an ``.ajax`` whose ``url`` is not a literal.

Method: ``$.get``/``getJSON``/``getScript``/``.load`` → GET, ``$.post`` → POST, ``$.ajax`` → the
settings' ``type`` (wins) else ``method`` else GET. A dynamic verb or URL is honest-null (never
guessed). Calls are validated on the AST; the byte checks are only a pre-filter.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit, apply
from ..treesitter import node_text, parse_source
from .sdk_calls import _emit_outbound, _walk_calls

_NAMES = frozenset({"$", "jQuery"})
_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
#: static helper → the verb it sends (``ajax`` is resolved from its settings).
_STATIC_VERB = {"get": "GET", "post": "POST", "getJSON": "GET", "getScript": "GET"}
_SCRIPT_REF = re.compile(rb"jquery[\w.\-]*\.js(?![\w])", re.IGNORECASE)


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type in ("await_expression", "parenthesized_expression"):
        node = node.named_children[0] if node.named_children else None
    return node


def _static_string(node: Node | None, source: bytes) -> str | None:
    """The value of a plain string / substitution-free template literal, else None."""
    if node is None:
        return None
    if node.type == "string" or (
        node.type == "template_string"
        and not any(c.type == "template_substitution" for c in node.named_children)
    ):
        text = node_text(node, source)
        return text[1:-1] if len(text) >= 2 else None
    return None


def _literal_url(node: Node | None, source: bytes) -> str | None:
    """A URL literal that is really a URL: a Razor ``@Url.Action(...)`` / WebForms ``<%= %>``
    expression inside a string is server-side code, not an endpoint — honest-null."""
    value = _static_string(node, source)
    if value is None or "<%" in value or value.lstrip().startswith("@"):
        return None
    return value


def _is_jquery_value(node: Node | None, source: bytes) -> bool:
    """``jQuery`` / ``window.jQuery`` / ``window.$`` / ``require('jquery')`` — a value that IS jQuery."""
    node = _unwrap(node)
    if node is None:
        return False
    text = node_text(node, source)
    if text in ("jQuery", "window.jQuery", "window.$", "globalThis.jQuery"):
        return True
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and node_text(fn, source) == "require" and args is not None:
            mod = _static_string(args.named_children[0] if args.named_children else None, source)
            return mod is not None and mod.lower().startswith("jquery")
    return False


def _jquery_callback_param(fn: Node, source: bytes) -> bool:
    """``jQuery(function($){…})`` / ``$(document).ready(function($){…})`` / the IIFE
    ``(function($){…})(jQuery)`` — a function whose ``$`` parameter jQuery itself supplies."""
    parent = fn.parent
    if parent is not None and parent.type == "parenthesized_expression":
        fn, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type == "arguments" and parent.parent is not None:  # callback to jQuery(...)/.ready(...)
        callee = parent.parent.child_by_field_name("function")
        text = node_text(callee, source) if callee is not None else ""
        return text in _NAMES or text.endswith(".ready")
    if parent.type == "call_expression" and parent.child_by_field_name("function") == fn:  # IIFE
        args = parent.child_by_field_name("arguments")
        return args is not None and any(_is_jquery_value(a, source) for a in args.named_children)
    return False


class _Scope:
    """File-level facts: which of ``$``/``jQuery`` the file rebinds to a non-jQuery value
    (``custom``), and whether the file shows evidence that jQuery is loaded (``evidence``)."""

    def __init__(self, root: Node, source: bytes, evidence_hint: bool) -> None:
        self.custom: set[str] = set()
        self.evidence = evidence_hint or bool(_SCRIPT_REF.search(source))
        self._scan(root, source)

    def _bind(self, name: str, jquery: bool) -> None:
        if name not in _NAMES:
            return
        if jquery:
            self.evidence = True  # `const $ = jQuery` / `import $ from 'jquery'`
        else:
            self.custom.add(name)

    def _scan(self, root: Node, source: bytes) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type == "import_statement":
                src = _static_string(n.child_by_field_name("source"), source)
                is_jq = src is not None and src.lower().startswith("jquery")
                if is_jq:
                    self.evidence = True
                for ident in self._import_names(n, source):
                    self._bind(ident, is_jq)
            elif n.type == "variable_declarator":
                name = n.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    self._bind(node_text(name, source), _is_jquery_value(n.child_by_field_name("value"), source))
                else:  # destructuring — unknown provenance for any $/jQuery it binds
                    for ident in self._pattern_names(name, source):
                        self._bind(ident, False)
            elif n.type == "call_expression":
                if _is_jquery_value(n, source):  # require('jquery') anywhere
                    self.evidence = True
            elif n.type in ("function_declaration", "class_declaration"):
                name = n.child_by_field_name("name")
                if name is not None:
                    self._bind(node_text(name, source), False)
            elif n.type == "arrow_function":  # `$ => …` (a lone, unparenthesised parameter)
                pat = n.child_by_field_name("parameter")
                if pat is not None and pat.type == "identifier" and node_text(pat, source) in _NAMES:
                    self._bind(node_text(pat, source), _jquery_callback_param(n, source))
            elif n.type in ("required_parameter", "optional_parameter"):
                pat = n.child_by_field_name("pattern")
                if pat is not None and pat.type == "identifier" and node_text(pat, source) in _NAMES:
                    params = n.parent  # formal_parameters → the function
                    fn = params.parent if params is not None else None
                    self._bind(node_text(pat, source), fn is not None and _jquery_callback_param(fn, source))

    @staticmethod
    def _import_names(imp: Node, source: bytes) -> list[str]:
        out: list[str] = []
        stack = list(imp.named_children)
        while stack:
            c = stack.pop()
            if c.type == "identifier":
                out.append(node_text(c, source))
            elif c.type == "import_specifier":
                alias = c.child_by_field_name("alias") or c.child_by_field_name("name")
                if alias is not None:
                    out.append(node_text(alias, source))
            elif c.type in ("import_clause", "named_imports", "namespace_import"):
                stack.extend(c.named_children)
        return out

    @staticmethod
    def _pattern_names(pat: Node | None, source: bytes) -> list[str]:
        out: list[str] = []
        stack = [pat] if pat is not None else []
        while stack:
            c = stack.pop()
            if c.type in ("identifier", "shorthand_property_identifier_pattern"):
                out.append(node_text(c, source))
            stack.extend(c.named_children)
        return out

    def trusted(self, name: str) -> bool:
        return name not in self.custom

    def is_jquery(self, name: str) -> bool:
        return self.trusted(name) and (name == "jQuery" or self.evidence)


def _pairs(obj: Node | None, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    if obj is None or obj.type != "object":
        return out
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        k, v = pair.child_by_field_name("key"), pair.child_by_field_name("value")
        if k is not None and v is not None:
            out.setdefault(node_text(k, source).strip("'\""), v)
    return out


def _verb(node: Node | None, source: bytes) -> str | None:
    value = _static_string(node, source)
    upper = value.upper() if value is not None else None
    return upper if upper in _VERBS else None


def _ajax(call_args: list[Node], source: bytes) -> tuple[str | None, str | None, bool]:
    """``(endpoint, method, options_have_literal_url)`` for ``$.ajax(options)`` /
    ``$.ajax(url, options)``. The flag is True only for an options *object* whose ``url`` is a
    literal — the one shape the no-evidence exception admits."""
    first = call_args[0] if call_args else None
    url_node: Node | None = None
    settings: Node | None = None
    if first is not None and first.type == "object":
        settings = first
        url_node = _pairs(first, source).get("url")
    elif first is not None:
        url_node = first
        settings = call_args[1] if len(call_args) > 1 else None
    pairs = _pairs(settings, source)
    verb_node = pairs.get("type") or pairs.get("method")  # `type` wins when both are given
    method = "GET" if verb_node is None else _verb(verb_node, source)
    url = _literal_url(url_node, source)
    return url, method, url is not None and first is not None and first.type == "object"


def detect_jquery_calls(root: Node, source: bytes, *, evidence_hint: bool = False) -> list[Emit]:
    """``api_call`` emits for every recognised jQuery AJAX call under ``root``.

    ``source`` is what ``root`` was parsed from. ``evidence_hint`` lets a caller that parsed only
    a ``<script>`` body vouch that jQuery is loaded by the surrounding page."""
    if b"$" not in source and b"jQuery" not in source:
        return []
    scope = _Scope(root, source, evidence_hint)
    out: list[Emit] = []
    for call in _walk_calls(root):
        fn = call.child_by_field_name("function")
        args_node = call.child_by_field_name("arguments")
        if fn is None or fn.type != "member_expression" or args_node is None:
            continue
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None or obj is None:
            continue
        method_name = node_text(prop, source)
        args = list(args_node.named_children)

        if method_name == "load":  # $(selector).load(url) — receiver must be a jQuery selection
            if obj.type != "call_expression":
                continue
            sel_fn = obj.child_by_field_name("function")
            recv = node_text(sel_fn, source) if sel_fn is not None and sel_fn.type == "identifier" else ""
            if recv not in _NAMES or not scope.is_jquery(recv):
                continue
            url = _literal_url(args[0] if args else None, source)
            if url is None:  # `$(window).load(fn)` is an event handler; only a literal URL is a request
                continue
            _emit_outbound(call, url, "jquery", out, method="GET", reclassify_db=True)
            continue

        if obj.type != "identifier" or node_text(obj, source) not in _NAMES:
            continue
        recv = node_text(obj, source)
        if not scope.trusted(recv):
            continue
        if method_name == "ajax":
            endpoint, method, literal_url = _ajax(args, source)
            if not literal_url and not scope.is_jquery(recv):
                continue  # the no-evidence exception needs an options object with a literal url
            _emit_outbound(call, endpoint or "", "jquery", out, method=method, reclassify_db=True)
        elif method_name in _STATIC_VERB and scope.is_jquery(recv):
            first = args[0] if args else None
            endpoint = _literal_url(first, source)
            if endpoint is None and first is not None and first.type == "object":
                endpoint = _literal_url(_pairs(first, source).get("url"), source)
            _emit_outbound(call, endpoint or "", "jquery", out,
                           method=_STATIC_VERB[method_name], reclassify_db=True)
    return out


# --- inline <script> blocks in markup (.cshtml / .aspx / .html) -------------------------
_SCRIPT_BLOCK = re.compile(rb"(<script\b([^>]*)>)(.*?)(</script\s*>)", re.IGNORECASE | re.DOTALL)
_SCRIPT_SRC = re.compile(rb"\bsrc\s*=\s*[\"']?([^\"'\s>]*)", re.IGNORECASE)
#: a ``type=`` that is not JavaScript (``text/template``, ``application/json``…) → not a code block.
_NON_JS_TYPE = re.compile(
    rb"\btype\s*=\s*(?![\"']?(?:(?:text|application)/(?:javascript|ecmascript)|module)\b)[\"']?\w",
    re.IGNORECASE,
)


def _shadow_scripts(markup: bytes) -> tuple[bytes, bool]:
    """``markup`` with everything except inline ``<script>`` bodies blanked (newlines kept), so
    offsets, lines and columns stay true to the original file; plus whether the page references a
    jQuery script by ``src`` (the evidence for ``$`` calls in its inline scripts)."""
    keep = bytearray(b" " * len(markup))
    for i, b in enumerate(markup):
        if b in b"\r\n":
            keep[i] = b
    evidence = False
    found = False
    for m in _SCRIPT_BLOCK.finditer(markup):
        attrs = m.group(2)
        src = _SCRIPT_SRC.search(attrs)
        if src is not None:
            evidence = evidence or bool(_SCRIPT_REF.search(src.group(1)))
        if src is not None or _NON_JS_TYPE.search(attrs):
            continue
        keep[m.start(3):m.end(3)] = markup[m.start(3):m.end(3)]
        found = True
    # a jQuery <script src> may also be a server expression (`~/Scripts/jquery.min.js`) — covered above
    return (bytes(keep) if found else b""), evidence


def detect_inline_jquery(record: FileRecord, markup: bytes, path: str, timeout_micros: int = 0) -> None:
    """Apply jQuery ``api_call`` statements for the inline ``<script>`` blocks of a markup file to
    ``record`` — the markup parsers' hook into this detector. Only called with statement capture."""
    if b"$" not in markup and b"jQuery" not in markup:
        return
    shadow, evidence = _shadow_scripts(markup)
    if not shadow:
        return
    root = parse_source("typescript", shadow, timeout_micros).root_node
    emits = detect_jquery_calls(root, shadow, evidence_hint=evidence)
    if emits:
        apply(record, emits, shadow, path)


def _detect(dc: DetectContext) -> Detection | None:
    emits = detect_jquery_calls(dc.root, dc.source)
    return Detection(emits, label="jquery") if emits else None


register_detector(Detector(
    name="jquery-calls", gate=Gate("typescript", order=35), detect=_detect,
    frameworks=("jquery",),
))
