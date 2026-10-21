"""Next.js **file-based** route-handler detection — both routers, API handlers only:

* **App Router** — a ``app/**/route.ts`` file whose module-level exports are HTTP-verb
  functions (``export async function GET(req){…}`` / ``export const POST = async (req) =>
  {…}``). One endpoint per exported verb; the URL is the directory path (the ``route.*``
  filename is not part of the URL).
* **Pages Router** — a ``pages/api/**`` file with a **default-exported** handler
  (``export default function handler(req, res){…}`` / ``export default (req,res) => {…}``).
  One endpoint per file; the URL includes the filename (``pages/api/users/[id].ts`` →
  ``/api/users/[id]``, ``index`` dropped). A Pages Router handler declares no verb — it
  branches on ``req.method`` internally — so ``method`` is ``ANY``.

UI files (``page.tsx``/``layout.tsx`` under ``app``; ``pages/*.tsx`` outside ``pages/api``)
are **not** handled here — they are UI pages, a separate modeling decision, not API routes.

Detection is **off the record** — the base :class:`TypeScriptParser` already extracts the
handler forms as top-level ``Function`` records, so this reads ``record.functions`` for the
authoritative ``parentId`` (never recomputing an id the base parser owns) and only walks the
top-level ``export_statement`` nodes to confirm the handler is genuinely *exported* (an
App Router verb must be exported; a Pages Router handler must be the *default* export). When
no handler function was extracted (an anonymous ``export default (req,res)=>{}``), the route
parents to the **file** (honest-null: File is a valid statement parent, and we never invent a
function id that has no node).

Endpoint derivation follows Next.js's documented convention, so it is exact, not a guess.
App Router: the segments between the ``app`` directory and the trailing ``route.*`` file.
Pages Router: the segments from ``pages`` onward, keeping the filename (minus extension,
``index`` collapsing to its directory). In both, **route groups** ``(group)`` and
**parallel-route slots** ``@slot`` are removed (they don't appear in the URL), and dynamic
segments are kept **literal** (``[id]``, ``[...slug]``, ``[[...slug]]``) — the on-disk path is
the honest route identity; we don't rewrite it into another framework's ``:id`` form.

**Pending ratification:** ``framework="nextjs"`` is NOT yet in the Code Ontology Parser
Target Spec's ``framework`` enum (§4.1) — the documented backend values are
``express``/``nestjs``/``spring``/``aspnet``/… with no ``nextjs``. Until it is added to the
spec enum and the backend Statement allow-list, the backend may drop this value at ingestion
(so a captured route would lose its framework, and the Routes view can't bucket it as
service/API). The parser emits the honest label deliberately; adding ``nextjs`` to the enum is
the tracked follow-up with the spec owner. Spec:
https://accionlabs.atlassian.net/wiki/x/BIAGl
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id, function_id
from ...schemas import FileRecord
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.base_paths import app_base_path
from .facts import next_base_paths, next_middlewares

_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ROUTE_STEMS = {"route"}  # App Router API handler file; page/layout are UI, not verb handlers


def _url_segments(segs: list[str]) -> str:
    """Join path segments into a URL, dropping route groups ``(x)`` and parallel slots
    ``@x`` (neither appears in the Next.js URL); dynamic segments are kept literal."""
    url = [s for s in segs if not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
    return "/" + "/".join(url) if url else "/"


def is_app_router_route_file(path: str) -> bool:
    """A file that can hold App Router verb handlers: ``…/app/**/route.<ext>``."""
    segs = path.replace("\\", "/").split("/")
    stem = segs[-1].rsplit(".", 1)[0]
    return stem in _ROUTE_STEMS and "app" in segs[:-1]


def is_pages_api_file(path: str) -> bool:
    """A file that can hold a Pages Router API handler: ``…/pages/api/**`` (any stem)."""
    segs = path.replace("\\", "/").split("/")[:-1]  # dirs only
    return "pages" in segs and segs[segs.index("pages") + 1 : segs.index("pages") + 2] == ["api"]


def endpoint_from_path(path: str) -> str:
    """URL for an App Router ``route.*`` file, per Next.js file-based routing.

    ``app/users/[id]/route.ts`` → ``/users/[id]`` · ``app/(admin)/stats/route.ts`` →
    ``/stats`` · ``app/route.ts`` → ``/``. Route groups ``(x)`` and parallel slots ``@x``
    are dropped; dynamic segments are kept literal.
    """
    segs = path.replace("\\", "/").split("/")
    app_idx = segs.index("app")  # first 'app' is the router root (src/app, apps/web/app, …)
    return _url_segments(segs[app_idx + 1 : -1])  # dirs between app/ and the route.* file


def pages_api_endpoint(path: str) -> str:
    """URL for a Pages Router ``pages/api/**`` file, per Next.js file-based routing.

    ``pages/api/users.ts`` → ``/api/users`` · ``pages/api/users/[id].ts`` →
    ``/api/users/[id]`` · ``pages/api/users/index.ts`` → ``/api/users`` ·
    ``pages/api/index.ts`` → ``/api``. The filename **is** part of the URL (unlike the
    App Router ``route.*`` sentinel); ``index`` collapses to its directory.
    """
    segs = path.replace("\\", "/").split("/")
    pages_idx = segs.index("pages")
    stem = segs[-1].rsplit(".", 1)[0]
    tail = [] if stem == "index" else [stem]
    return _url_segments(segs[pages_idx + 1 : -1] + tail)  # 'api' + dirs + filename (no index)


def _exported_verb_lines(root: Node, source: bytes) -> dict[str, int]:
    """Exported HTTP-verb handlers → the start line of the function/arrow node the base
    parser used for its id. Covers ``export [async] function GET(){}`` and
    ``export const GET = () => {}`` (both forms are what Next.js accepts)."""
    out: dict[str, int] = {}
    for child in root.named_children:
        if child.type != "export_statement":
            continue
        for decl in child.named_children:
            if decl.type == "function_declaration":
                name_node = decl.child_by_field_name("name")
                name = node_text(name_node, source) if name_node else ""
                if name in _VERBS:
                    out[name] = decl.start_point[0] + 1  # build_function spans the decl
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                for vd in decl.named_children:
                    if vd.type != "variable_declarator":
                        continue
                    name_node = vd.child_by_field_name("name")
                    value = vd.child_by_field_name("value")
                    name = node_text(name_node, source) if name_node else ""
                    if (
                        name in _VERBS
                        and value is not None
                        and value.type
                        in (
                            "arrow_function",
                            "function_expression",
                            "function",
                        )
                    ):
                        out[name] = value.start_point[0] + 1  # build_function spans the value
    return out


_FN_VALUE_TYPES = ("arrow_function", "function_expression", "function")


def _default_export_handler(root: Node, source: bytes) -> tuple[str | None, int, str] | None:
    """The Pages Router default-export handler as ``(name, start_line, kind)``:

    * ``kind="decl"`` — ``export default function handler(){}`` (name, always a function)
    * ``kind="ref"``  — ``export default handler;`` (name is an identifier that MUST be
      confirmed to resolve to a function before it counts — it could reference an object)
    * ``kind="anon"`` — ``export default (req,res)=>{}`` (name ``None``, inline function)

    Returns ``None`` when the file has no default export, or the default export is a value
    that cannot be a handler (an object/array/literal ``export default {...}``) — those are
    dropped outright, never emitted as routes. The line is the node the base parser spans."""
    for child in root.named_children:
        if child.type != "export_statement":
            continue
        if not any(c.type == "default" for c in child.children):
            continue  # a named export, not the default handler
        for decl in child.named_children:
            if decl.type == "function_declaration":  # export default function handler(){}
                name_node = decl.child_by_field_name("name")
                return (
                    node_text(name_node, source) if name_node else None,
                    decl.start_point[0] + 1,
                    "decl",
                )
            if decl.type == "identifier":  # export default handler;  (ref — must resolve to a fn)
                return (node_text(decl, source), decl.start_point[0] + 1, "ref")
            if decl.type in _FN_VALUE_TYPES:  # export default (req,res)=>{}  (anonymous)
                return (None, decl.start_point[0] + 1, "anon")
    return None


def _served(endpoint: str, path: str, index: object | None) -> str | None:
    """The route's address under its app's ``next.config`` ``basePath``: joined in front when it
    is a literal; unknown (None) when it is set but cannot be resolved; unchanged when the app
    sets none or no ``next.config`` was seen."""
    found, base = app_base_path(next_base_paths(index), path)
    if not found or base == "":
        return endpoint
    if base is None:
        return None
    parts = [p.strip("/") for p in (base, endpoint) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _pattern_covers(pattern: str, endpoint: str) -> bool:
    """Whether a middleware ``matcher`` pattern matches EVERY URL of a route. Supports Next's
    simple forms — literal segments, ``:name``, ``:name?`` and a final ``:name*`` / ``:name+``.
    A regex-like pattern, or a route dynamic segment (``[id]``) under a literal pattern
    segment (only some of its URLs match), is not certain coverage → False."""
    if not pattern.startswith("/") or any(c in pattern for c in "()[]{}\\") or ".*" in pattern:
        return False
    psegs = [s for s in pattern.split("/") if s]
    rsegs = [s for s in endpoint.split("/") if s]
    for i, ps in enumerate(psegs):
        last = i == len(psegs) - 1
        if ps.startswith(":") and ps.endswith(("*", "+")):
            if not last:
                return False
            rest = rsegs[i:]
            return ps.endswith("*") or (bool(rest) and not rest[0].startswith("[[..."))
        if i >= len(rsegs):
            return last and ps.startswith(":") and ps.endswith("?")
        rs = rsegs[i]
        if rs.startswith(("[...", "[[...")):  # catch-all: only a final :x* / :x+ covers it
            return False
        if ps.startswith(":"):
            continue
        if rs.startswith("[") or rs != ps:  # literal pattern vs dynamic / different segment
            return False
    return len(psegs) == len(rsegs)


def _middleware_guards(index: object | None, path: str, local: str) -> list[str]:
    """``["middleware"]`` when the app's ``middleware.(ts|js)`` certainly runs before this route:
    the route's app directory (the deepest directory above it holding a ``next.config`` or a
    middleware) has a middleware, and it has no matcher or one of its patterns covers every URL
    of the route (``local``: the file-system address, before ``basePath``)."""
    mws = next_middlewares(index)
    if not mws:
        return []
    configs = next_base_paths(index)
    anchors = [d for d in {*mws, *configs} if d == "" or path.startswith(d + "/")]
    if not anchors:
        return []
    app = max(anchors, key=len)
    if app not in mws:
        return []
    patterns = mws[app]
    if patterns is None or any(_pattern_covers(p, local) for p in patterns):
        return ["middleware"]
    return []


def _route(
    root: Node,
    endpoint: str | None,
    method: str,
    handler: str | None,
    parent: str,
    handler_line: int,
    end_line: int,
    *,
    local: str,
    guards: list[str] | None = None,
) -> Emit:
    return Emit(
        root,
        {
            "semanticType": "route",
            "framework": "nextjs",  # pending spec §4.1 enum ratification — see module docstring
            "method": method,
            "endpoint": endpoint,
            "handler": handler,
            "handlerLine": handler_line,
            "guards": guards or None,
            "routeKind": "route",
            "isRegex": False,
        },
        parent=parent, node_type="synthetic", text=f"{method} {local}",
        line=handler_line, col=0, end_line=end_line,
    )


def detect_nextjs_routes(
    root: Node, source: bytes, path: str, record: FileRecord, *, index: object | None = None,
) -> list[Emit]:
    """The route handlers of an App Router ``route.*`` or Pages Router ``pages/api`` file, for
    :func:`~..emits.apply`. ``index`` is the repo's ``TsAliasIndex``: it carries each app's
    ``next.config`` ``basePath``, joined in front of the route's file-system address."""
    fid = file_id(path)
    # authoritative handler ids from the base extraction (top-level functions only)
    fn_by_name = {f.name: f for f in record.functions if f.parentId == fid}

    if is_app_router_route_file(path):
        verb_lines = _exported_verb_lines(root, source)
        endpoint = endpoint_from_path(path)
        routes: list[Emit] = []
        for verb, line in verb_lines.items():
            fn = fn_by_name.get(verb)
            # Match the base parser's id when the handler was extracted; otherwise fall back
            # to the id it would have assigned (same emit convention) — never fabricate.
            parent = fn.id if fn is not None else function_id(path, verb, line)
            handler_line = fn.startLine if fn is not None else line
            end_line = fn.endLine if fn is not None else handler_line
            routes.append(
                _route(
                    root,
                    _served(endpoint, path, index),
                    verb,
                    verb,
                    parent,
                    handler_line,
                    end_line,
                    local=endpoint,
                    guards=_middleware_guards(index, path, endpoint),
                )
            )
        return routes

    if is_pages_api_file(path):
        found = _default_export_handler(root, source)
        if found is None:
            return []  # no default export, or a non-function default (object/literal) → not a handler
        name, line, kind = found
        fn = fn_by_name.get(name) if name else None
        # A bare `export default someName` only counts if the name resolves to a function the
        # base parser extracted — otherwise it references an object/const, not a handler (drop it,
        # no guess). A `function` decl always resolves; an anonymous inline fn has no name.
        if kind == "ref" and fn is None:
            return []
        # decl/ref → the extracted Function's id; anonymous inline fn has no Function node →
        # parent to the file (honest-null: never invent a function id with no backing node).
        parent = fn.id if fn is not None else fid
        handler_line = fn.startLine if fn is not None else line
        end_line = fn.endLine if fn is not None else handler_line
        # Pages Router handlers branch on req.method internally — no declared verb → ANY.
        return [
            _route(
                root,
                _served(pages_api_endpoint(path), path, index),
                "ANY",
                name,
                parent,
                handler_line,
                end_line,
                local=pages_api_endpoint(path),
                guards=_middleware_guards(index, path, pages_api_endpoint(path)),
            )
        ]

    return []
