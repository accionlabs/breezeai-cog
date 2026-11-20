"""Addresses with a part read at runtime: one notation for routes and outbound calls.

Apps often build an address from a value cog cannot read — a context root, base path or prefix
from config, env or a settings object — plus a literal path::

    router.get(pathPrefix + "/request")          // inbound
    axios.post(config.contextRoot + "/orders")    // outbound

cog never guesses that value, but it keeps the address with the value marked where it stands:
``${pathPrefix}/request``, ``${contextRoot}/orders``. The two markers mean different things:

- ``${name}`` — a value from code or config that cog cannot read. It may be empty, one path
  segment, several, or a scheme and host. Fixed for a deployment, never filled by a caller.
- ``{name}`` (and a framework's own ``:id``, ``<id>``) — a path parameter: one segment, filled
  per request. Unchanged.

Which one a runtime part becomes:

- in an **inbound** route path, every part cog cannot read is ``${name}`` (a request value enters
  a route only through its own parameter syntax, which is kept as written);
- in an **outbound** URL, a part in the base (before the path's first ``/``, the scheme and
  host included) is ``${name}``; a part after it is a request value, ``{name}``.

An address with no literal path part (``${host}${path}``, ``${base}/``) says nothing: ``endpoint``
is then empty. ``name`` is the value's own name (``config.contextRoot`` → ``contextRoot``), the
key it is read with (``config.getString("pathPrefix")`` → ``pathPrefix``), else ``value``.
"""

from __future__ import annotations

import re

from ..schemas import FileRecord

#: Outbound records whose ``endpoint`` is a URL.
_OUTBOUND = frozenset({"api_call"})

#: Opens a runtime part: ``${name}``.
RUNTIME_OPEN = "${"

#: A ``${name}`` part.
_RUNTIME_PART = re.compile(r"\$\{[^{}]*\}")
#: A ``{name}`` part that is not already ``${name}``.
_PLACEHOLDER = re.compile(r"(?<!\$)\{([^{}]*)\}")
_SIMPLE = re.compile(r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*")
_KEYED = re.compile(r"""\(\s*["'`]([\w.\-]+)["'`]\s*[,)]""")
_CALLED = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")


def runtime_name(expr_text: str) -> str:
    """A short name for a value read at runtime, from its source text: a name or member path
    gives its last part (``config.contextRoot`` → ``contextRoot``, ``this.prefix`` →
    ``prefix``); a lookup by a literal key gives the key (``config().getString("pathPrefix")``,
    ``os.getenv("BASE")`` → ``pathPrefix``, ``BASE``); another call gives the called name
    (``prefixFromEnv()`` → ``prefixFromEnv``); anything else is ``value``."""
    text = expr_text.strip()
    if _SIMPLE.fullmatch(text):
        return re.split(r"\s*\.\s*", text)[-1]
    keyed: list[str] = _KEYED.findall(text)
    if keyed:
        return keyed[-1]
    called: list[str] = _CALLED.findall(text)
    return called[-1] if called else "value"


def runtime_part(expr_text: str) -> str:
    """``${name}`` for a value read at runtime (:func:`runtime_name`)."""
    return RUNTIME_OPEN + runtime_name(expr_text) + "}"


def has_runtime(address: str | None) -> bool:
    """Whether an address holds a ``${name}`` part."""
    return address is not None and RUNTIME_OPEN in address


def with_literal(address: str | None) -> str | None:
    """``address``, or None when it has no literal path part besides its runtime parts and
    separators (``${host}${path}``, ``${base}/``): such an address says nothing."""
    if address is None:
        return None
    rest = _PLACEHOLDER.sub("", _RUNTIME_PART.sub("", address))
    return address if rest.strip("/:.?&=# ") else None


def mark_runtime(rendered: str) -> str:
    """Every ``{name}`` placeholder of a rendered path as ``${name}``: for a path whose
    placeholders all stand for values read at runtime (a route path or a mount prefix, where
    no route parameter is ever rendered as ``{name}``)."""
    return _PLACEHOLDER.sub(lambda m: runtime_part(m.group(1)), rendered)


def inbound_template(rendered: str | None) -> str | None:
    """A rendered route path with its placeholders marked (:func:`mark_runtime`); None when no
    literal path part is left."""
    return with_literal(mark_runtime(rendered)) if rendered is not None else None


def join_runtime(base: str, path: str) -> str:
    """A base holding a runtime part joined with a path by one ``/`` (``${prefix}`` +
    ``/x`` → ``${prefix}/x``): never a ``/`` put in front of the runtime part."""
    return base.rstrip("/") + "/" + path.lstrip("/") if path.strip("/") else base


def outbound_template(url: str | None) -> str | None:
    """A rendered outbound URL: each ``{name}`` in its base — up to the first ``/`` of the path,
    the scheme and host included — becomes ``${name}``; the ones after it stay ``{name}``
    (request values). None when no literal path part is left. A URL with no placeholder in its
    base is returned as it is."""
    if not url:
        return url
    start = url.find("://")
    start = start + 3 if start != -1 else 0
    slash = url.find("/", start)
    end = len(url) if slash == -1 else slash
    base = url[:end]
    if not _PLACEHOLDER.search(base):
        return url
    base = _PLACEHOLDER.sub(lambda m: runtime_part(m.group(1)), base)
    return with_literal(base + url[end:])


def normalize_addresses(record: FileRecord) -> None:
    """Once per file, for every language: an outbound URL with a runtime part in its base gets
    that part marked ``${name}`` (:func:`outbound_template`); a route address that starts with
    a runtime part loses the ``/`` a path join put in front of it (``/${base}/x`` →
    ``${base}/x``: the value may hold its own ``/``, a scheme or a host). An address with no
    literal path part left is cleared."""
    for stmt in record.statements:
        endpoint = stmt.endpoint
        if not endpoint or "{" not in endpoint:
            continue
        if stmt.semanticType in _OUTBOUND:
            stmt.endpoint = outbound_template(endpoint)
        elif stmt.semanticType == "route" and RUNTIME_OPEN in endpoint:
            if endpoint.startswith("/" + RUNTIME_OPEN):
                endpoint = endpoint[1:]
            stmt.endpoint = with_literal(endpoint)
