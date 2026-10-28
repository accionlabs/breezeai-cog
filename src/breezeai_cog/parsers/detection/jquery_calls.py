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

**Options-object exception:** ``$.ajax({ url: …, … })`` is recognised without evidence — an options
object literal carrying a ``url`` property is a deliberate, narrow signal (the URL may be dynamic:
``url``, ``form.action`` → endpoint null). It never applies to ``get``/``post``/…, to ``.load``,
to ``$.ajax(settings)`` / ``$.ajax($.extend(…))`` (settings not visible), or to a rebound ``$``.

Method: ``$.get``/``getJSON``/``getScript``/``.load`` → GET, ``$.post`` → POST, ``$.ajax`` → the
settings' ``type`` (wins) else ``method`` else GET. A dynamic verb or URL is honest-null (never
guessed). Calls are validated on the AST; the byte checks are only a pre-filter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector, register_fact,
)
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


#: A server expression inside a string: WebForms ``<%`` anywhere, or a Razor ``@`` that starts an
#: expression (``@Model.Id``, ``@(…)``, ``@{``). ``@@`` is Razor's escaped literal ``@``, and an ``@``
#: glued to a preceding word character (``a@b.com``, ``user@host``) is not an expression.
_SERVER_EXPR = re.compile(r"<%|(?<![\w.\-@])@(?=[A-Za-z_(\{])")
_ARG_END = frozenset({",", ")", "}"})


def _delimited(node: Node) -> bool:
    """Is ``node`` a whole argument / property value? A server expression the JavaScript grammar
    cannot read (``"/a/" + @Model.Id``, ``"/a/" + <%= Id %>``) is recovered as the string followed by
    an ERROR node — the string alone is then only a *prefix*, not the URL."""
    if node.parent is not None and node.parent.type == "pair":
        node = node.parent
    nxt = node.next_sibling
    return nxt is None or nxt.type in _ARG_END


def _literal_url(node: Node | None, source: bytes) -> str | None:
    """A URL literal that is really a URL: a Razor ``@Url.Action(...)`` / ``@Model.Id`` or WebForms
    ``<%= %>`` expression in or beside the string is server-side code, not an endpoint — honest-null."""
    value = _static_string(node, source)
    if value is None or node is None or not _delimited(node):
        return None
    if _SERVER_EXPR.search(value) or value.lstrip().startswith("@"):
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


def _ajax(call_args: list[Node], source: bytes) -> tuple[str | None, str | None, bool] | None:
    """``(endpoint, method, options_have_url)`` for ``$.ajax(options)`` /
    ``$.ajax(url, options)`` / ``$.ajax(url)``, or ``None`` when the request is not statically
    visible (``$.ajax(opts)``, ``$.ajax($.extend(…))``). The flag is True only for an options
    *object literal with a ``url`` property* (literal or not) — the one shape the no-evidence
    exception admits: the call's own shape is the evidence, and a dynamic ``url`` (``url``,
    ``form.action``) is still an outbound request whose endpoint is just unresolved.

    The verb is read from ``type`` (wins) then ``method``; absent from a literal options object it
    is jQuery's default ``GET``; unreadable (dynamic value, or a ``...spread`` that may carry it)
    it is ``None`` — never guessed."""
    first = call_args[0] if call_args else None
    if first is None:
        return None
    has_url_option = False
    if first.type == "object":
        settings: Node | None = first
        url_node = _pairs(first, source).get("url")
        has_url_option = url_node is not None
    elif len(call_args) > 1 and call_args[1].type == "object":
        settings, url_node = call_args[1], first
    elif first.type in ("string", "template_string"):
        settings, url_node = None, first  # $.ajax("/x"): no settings at all → default GET
    else:
        return None
    pairs = _pairs(settings, source)
    verb_node = pairs.get("type") or pairs.get("method")
    if verb_node is not None:
        method = _verb(verb_node, source)
    elif settings is not None and any(c.type == "spread_element" for c in settings.named_children):
        method = None
    else:
        method = "GET"
    url = _literal_url(url_node, source)
    return url, method, has_url_option


def _url_like(node: Node | None, source: bytes) -> bool:
    """A first ``.load`` argument that reads as a URL expression (a string, or a template /
    concatenation building one) rather than a handler. A bare identifier or member is ambiguous
    (``.load(onLoad)`` is the legacy load *event*), so it is not treated as a URL."""
    if node is None:
        return False
    return node.type in ("string", "binary_expression") or node.type == "template_string"


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
            sel_args = obj.child_by_field_name("arguments")
            sel = node_text(sel_args.named_children[0], source) if sel_args is not None and sel_args.named_children else ""
            if sel in ("window", "document"):  # $(window).load(fn) is the legacy load event
                continue
            first = args[0] if args else None
            url = _literal_url(first, source)
            if url is None and not _url_like(first, source):
                continue  # a function / handler / no argument is the event form, not a request
            _emit_outbound(call, url or "", "jquery", out, method="GET", enrich="own", reclassify_db=True)
            continue

        if obj.type != "identifier" or node_text(obj, source) not in _NAMES:
            continue
        recv = node_text(obj, source)
        if not scope.trusted(recv):
            continue
        if method_name == "ajax":
            shape = _ajax(args, source)
            if shape is None:
                continue
            endpoint, method, has_url_option = shape
            if not has_url_option and not scope.is_jquery(recv):
                continue  # the no-evidence exception needs an options object with a `url` property
            _emit_outbound(call, endpoint or "", "jquery", out, method=method, enrich="own", reclassify_db=True)
        elif method_name in _STATIC_VERB and scope.is_jquery(recv):
            first = args[0] if args else None
            endpoint = _literal_url(first, source)
            if endpoint is None and first is not None and first.type == "object":
                endpoint = _literal_url(_pairs(first, source).get("url"), source)
            _emit_outbound(call, endpoint or "", "jquery", out,
                           method=_STATIC_VERB[method_name], enrich="own", reclassify_db=True)
    return out


# --- inline <script> blocks in markup (.cshtml / .aspx / .html) -------------------------
#: the attributes of an open tag, which may hold server code: ``<% … %>`` blocks and quoted values
#: (``src="@Url.Content("~/x.js")"`` is adjacent quoted / unquoted runs), so the ``>`` of a ``%>``
#: inside the tag does not end it early. Bounded (no real tag has 2048 attribute tokens), so a
#: pathological run of unterminated ``<script `` openers cannot make the search quadratic.
_OPEN_ATTRS = rb"((?:<%.*?%>|\"[^\"]*\"|'[^']*'|[^>\"'<]|<(?!%)){0,2048})>"
_SCRIPT_OPEN = re.compile(rb"<script\b" + _OPEN_ATTRS, re.IGNORECASE | re.DOTALL)
_SCRIPT_CLOSE = re.compile(rb"</script\s*>", re.IGNORECASE)
_SRC_ATTR = re.compile(rb"\bsrc\s*=", re.IGNORECASE)
#: a ``type=`` that is not JavaScript (``text/template``, ``application/json``…) → not a code block.
_NON_JS_TYPE = re.compile(
    rb"\btype\s*=\s*(?![\"']?(?:(?:text|application)/(?:javascript|ecmascript)|module)\b)[\"']?\w",
    re.IGNORECASE,
)
_HTML_COMMENT = (b"<!--", b"-->")
_RAZOR_COMMENT = (b"@*", b"*@")
_WEBFORMS_COMMENT = (b"<%--", b"--%>")
_NOT_NEWLINE = re.compile(rb"[^\r\n]")


def _server_comments(path: str) -> tuple[bytes, bytes] | None:
    """The server-side comment syntax of ``path``'s dialect. Such a comment is removed before the page
    is sent — wherever it sits, even inside a ``<script>`` — so it exists only for the dialect that
    owns it (``@*`` in a plain ``.html`` JavaScript string is just text). ``<!-- -->`` is different:
    an HTML comment, markup only, never inside a script body."""
    low = path.lower()
    if low.endswith((".cshtml", ".razor")):
        return _RAZOR_COMMENT
    if low.endswith((".aspx", ".ascx", ".master")):
        return _WEBFORMS_COMMENT
    return None


def _blank(buf: bytearray, start: int, end: int) -> None:
    """Space out ``buf[start:end]``, keeping newlines so offsets, lines and columns stay true."""
    buf[start:end] = _NOT_NEWLINE.sub(b" ", bytes(buf[start:end]))


def _sweep(markup: bytes, path: str) -> tuple[bytearray, list[tuple[int, int, bytes]]]:
    """One left-to-right pass over ``markup``: a copy with every comment blanked, plus the
    ``(body_start, body_end, attributes)`` of each ``<script>`` that is live (not inside a comment).
    Walking in order is what lets a comment hide a ``<script>`` while a ``<script>`` body hides an
    ``<!--`` that is merely JavaScript text."""
    server = _server_comments(path)
    closers = dict([_HTML_COMMENT] + ([server] if server else []))
    opener = re.compile(b"|".join([rb"<!--", rb"<script\b"] + [re.escape(o) for o in closers if o != b"<!--"]),
                        re.IGNORECASE)
    clean = bytearray(markup)
    scripts: list[tuple[int, int, bytes]] = []
    pos = 0
    while (m := opener.search(markup, pos)) is not None:
        tok = m.group(0)
        if not tok.lower().startswith(b"<script"):  # a comment: blank it up to its close (or EOF)
            end = markup.find(closers[tok], m.end())
            end = len(markup) if end < 0 else end + len(closers[tok])
            _blank(clean, m.start(), end)
            pos = end
            continue
        tag = _SCRIPT_OPEN.match(markup, m.start())
        close = _SCRIPT_CLOSE.search(markup, tag.end()) if tag is not None else None
        if tag is None or close is None:
            pos = m.end()
            continue
        if server:  # a server comment inside the body vanishes before the browser sees it
            rx = re.compile(re.escape(server[0]) + rb".*?" + re.escape(server[1]), re.DOTALL)
            for c in rx.finditer(markup, tag.end(), close.start()):
                _blank(clean, c.start(), c.end())
        scripts.append((tag.end(), close.start(), tag.group(1)))
        pos = close.end()
    return clean, scripts


def _shadow_scripts(markup: bytes, path: str = "") -> tuple[bytes, bool]:
    """``markup`` with everything except inline ``<script>`` bodies blanked (newlines kept), so
    offsets, lines and columns stay true to the original file; plus whether the page loads jQuery
    (the evidence for ``$`` calls in its inline scripts). Commented-out markup is dead: neither its
    scripts nor its jQuery references count."""
    clean, scripts = _sweep(markup, path)
    keep = bytearray(_NOT_NEWLINE.sub(b" ", markup))
    found = False
    for body_start, body_end, attrs in scripts:
        if _SRC_ATTR.search(attrs) or _NON_JS_TYPE.search(attrs):
            continue
        keep[body_start:body_end] = clean[body_start:body_end]
        found = True
    return (bytes(keep) if found else b""), _page_loads_jquery(bytes(clean))


# --- project-level evidence: jQuery loaded by a page/layout, not by the file under analysis ---
# A request often lives where jQuery is not referenced: an external script (``site.js``) or a page
# whose shared layout loads the library. Two bounded, framework-conventional lookups cover it,
# neither a repo-wide scan:
#   * a markup file's ``<script src>`` list (``_script_refs``): a script listed AFTER jQuery runs
#     with it loaded — recorded as a fact so the script's own parse can read it (``_collect``);
#   * a Razor view's layout chain (``Layout = "…"`` in the view, else the nearest ``_ViewStart``),
#     resolved with ASP.NET's own view-location rule (``_find_layout``).
_MARKUP_EXTS = (".cshtml", ".razor", ".html", ".htm", ".aspx", ".ascx", ".master")
_SCRIPT_TAG = _SCRIPT_OPEN
#: ``<asp:ScriptReference Path="~/Scripts/jquery.js" />`` — a ScriptManager's script list
_SCRIPT_REFERENCE = re.compile(rb"<asp:ScriptReference\b" + _OPEN_ATTRS, re.IGNORECASE | re.DOTALL)
#: ``Scripts.Render("~/bundles/jquery")`` — ASP.NET's bundle for jQuery itself, by the template's
#: own name. ``~/bundles/jqueryval`` (validation) and other bundles are not it.
_JQUERY_BUNDLE = re.compile(
    rb"\bScripts\s*\.\s*(?:Render|Url)\s*\([^)]*[\"']~/bundles/jquery[\"']", re.IGNORECASE)
#: the first ``*.js`` path in an attribute value — also inside ``@Url.Content("~/js/x.js")``
_JS_PATH = re.compile(rb"(?:https?:)?[\w.~/\-]*[\w\-]\.js(?![\w])", re.IGNORECASE)
_LAYOUT_ASSIGN = re.compile(rb'\bLayout\s*=\s*"([^"]+)"')
_LAYOUT_NULL = re.compile(rb"\bLayout\s*=\s*null\b")
_LAYOUT_DEPTH = 3  # a layout may itself have a parent layout


def _script_refs(markup: bytes) -> list[tuple[str, bool]]:
    """``(normalised path, is_external)`` of each ``<script src>`` in order. A path is lower-cased
    with ``~/``, ``/`` and ``../`` roots dropped, so it can be suffix-matched to a repo file."""
    out: list[tuple[str, bool]] = []
    for tag in _SCRIPT_TAG.finditer(markup):
        attrs = tag.group(1)
        src = _SRC_ATTR.search(attrs)
        path = _JS_PATH.search(attrs, src.end()) if src is not None else None
        if path is None:
            continue
        ref = path.group(0).decode("utf-8", "replace").lower()
        external = ref.startswith(("http:", "https:", "//"))
        while ref.startswith(("~/", "/", "../", "./")):
            ref = ref[2:] if ref.startswith(("~/", "../", "./")) else ref[1:]
        out.append((ref, external))
    return out


def _is_jquery_ref(ref: str) -> bool:
    return _SCRIPT_REF.search(ref.encode()) is not None


def _bundle_loads_jquery(markup: bytes) -> bool:
    """``Scripts.Render("~/bundles/jquery")`` or an ``<asp:ScriptReference>`` to a jQuery file."""
    if _JQUERY_BUNDLE.search(markup) is not None:
        return True
    for tag in _SCRIPT_REFERENCE.finditer(markup):
        path = _JS_PATH.search(tag.group(1))
        if path is not None and _is_jquery_ref(path.group(0).decode("utf-8", "replace")):
            return True
    return False


def _page_loads_jquery(clean: bytes) -> bool:
    """Does comment-free ``clean`` markup load jQuery — a jQuery ``<script src>`` (a server
    expression around the path is fine: ``<%= ResolveUrl("~/Scripts/jquery.js") %>``,
    ``@Url.Content(…)``), a jQuery ``<asp:ScriptReference>``, or the ``~/bundles/jquery`` bundle?
    An unrelated server expression is not evidence: it must name jQuery."""
    return any(_is_jquery_ref(ref) for ref, _ in _script_refs(clean)) or _bundle_loads_jquery(clean)


def _layout_names(markup: bytes) -> list[str]:
    return [m.group(1).decode("utf-8", "replace") for m in _LAYOUT_ASSIGN.finditer(markup)]


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _find_layout(name: str, base: Path, repo_root: Path) -> Path | None:
    """The layout file ``name`` refers to, searched like ASP.NET does: the view's directory and each
    ancestor up to the repo root, directly and under a ``Shared`` folder (``Pages/Shared``,
    ``Views/Shared``)."""
    rel = Path(name.strip().lstrip("~").lstrip("/"))
    if not rel.name or ".." in rel.parts:
        return None
    if rel.suffix.lower() != ".cshtml":
        rel = rel.with_name(rel.name + ".cshtml")
    anc = base
    while _within(anc, repo_root):
        for sub in ("", "Shared"):
            cand = anc / sub / rel
            if cand.is_file():
                return cand
        if anc == repo_root:
            break
        anc = anc.parent
    return None


def _view_start_layouts(base: Path, repo_root: Path) -> list[str]:
    """``Layout`` names from the nearest ``_ViewStart`` that assigns one (it applies to every view
    beneath it)."""
    anc = base
    while _within(anc, repo_root):
        try:
            names = _layout_names((anc / "_ViewStart.cshtml").read_bytes())
        except OSError:
            names = []
        if names:
            return names
        if anc == repo_root:
            break
        anc = anc.parent
    return []


def _chain_loads_jquery(names: list[str], base: Path, repo_root: Path, depth: int = 0) -> bool:
    for name in names:
        layout = _find_layout(name, base, repo_root)
        if layout is None:
            continue
        try:
            data = layout.read_bytes()
        except OSError:
            continue
        if _page_loads_jquery(bytes(_sweep(data, layout.name)[0])):
            return True
        if depth < _LAYOUT_DEPTH and _chain_loads_jquery(
            _layout_names(data), layout.parent, repo_root, depth + 1
        ):
            return True
    return False


def layout_loads_jquery(markup: bytes, rel: str, repo_root: Path) -> bool:
    """Does the Razor view at ``rel`` render inside a layout that loads jQuery? Partials count too:
    they are rendered into pages, and ``_ViewStart`` applies to the views under it. ``Layout = null``
    opts out."""
    if _LAYOUT_NULL.search(markup):
        return False
    base = (repo_root / rel).parent
    names = _layout_names(markup) or _view_start_layouts(base, repo_root)
    return _chain_loads_jquery(names, base, repo_root)


def _collect(root: Any, source: bytes, rel: str, repo_root: Path | None) -> frozenset[str] | None:
    """Fact value for one markup file: the repo-file script paths it loads WITH jQuery available —
    those listed after a jQuery ``<script src>``, or all of them when its layout loads jQuery."""
    if not rel.lower().endswith(_MARKUP_EXTS):
        return None
    clean = bytes(_sweep(source, rel)[0])  # a commented-out <script src> loads nothing
    refs = _script_refs(clean)
    first = next((i for i, (ref, _) in enumerate(refs) if _is_jquery_ref(ref)), None)
    if first is not None:
        loaded = refs[first + 1:]
    elif refs and (_bundle_loads_jquery(clean)
                   or (repo_root is not None and layout_loads_jquery(source, rel, repo_root))):
        loaded = refs
    else:
        return None
    paths = frozenset(ref for ref, external in loaded if not external and not _is_jquery_ref(ref))
    return paths or None


def _resolve(values: list[Any], index: Any) -> frozenset[str] | None:
    merged = frozenset().union(*values) if values else frozenset()
    return merged or None


def _loaded_with_jquery(path: str, loaded: frozenset[str] | None) -> bool:
    low = path.lower()
    return bool(loaded) and any(low == ref or low.endswith("/" + ref) for ref in loaded)  # type: ignore[union-attr]


def detect_inline_jquery(
    record: FileRecord, markup: bytes, path: str, timeout_micros: int = 0,
    repo_root: Path | None = None,
) -> None:
    """Apply jQuery ``api_call`` statements for the inline ``<script>`` blocks of a markup file to
    ``record`` — the markup parsers' hook into this detector. Only called with statement capture.
    ``repo_root`` (Razor) lets the view's layout chain supply the jQuery evidence."""
    if b"$" not in markup and b"jQuery" not in markup:
        return
    shadow, evidence = _shadow_scripts(markup, path)
    if not shadow:
        return
    if not evidence and repo_root is not None:
        evidence = layout_loads_jquery(markup, path, repo_root)
    root = parse_source("typescript", shadow, timeout_micros).root_node
    emits = detect_jquery_calls(root, shadow, evidence_hint=evidence)
    if emits:
        apply(record, emits, shadow, path)


def _detect(dc: DetectContext) -> Detection | None:
    loaded = index_fact(dc.ctx.resolution_index, _FACT)
    emits = detect_jquery_calls(
        dc.root, dc.source, evidence_hint=_loaded_with_jquery(dc.path, loaded)
    )
    return Detection(emits, label="jquery") if emits else None


_FACT = "jquery.loaded-scripts"

register_fact(FactSpec(
    name=_FACT, language="typescript", gate=(b"<script", b"<SCRIPT"),
    collect=_collect, resolve=_resolve, sources=_MARKUP_EXTS,
))

register_detector(Detector(
    name="jquery-calls", gate=Gate("typescript", order=35), detect=_detect,
    frameworks=("jquery",),
))
