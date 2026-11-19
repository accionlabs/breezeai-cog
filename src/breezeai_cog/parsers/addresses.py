"""Addresses whose leading part is a runtime value: one rule for routes and outbound calls.

Apps often build an address from a value read at runtime (a context root, base path or prefix
from config, env or a settings object) plus a literal path::

    router.get(pathPrefix + "/request")          // inbound
    axios.post(config.contextRoot + "/orders")    // outbound

cog cannot know the prefix, and must not guess it. It keeps the part written in the code:
``endpoint`` stays empty (the address is not resolved) and ``endpointSuffix`` holds the known
tail, ``/request`` / ``/orders``. The suffix always starts with ``/``. The unknown prefix is
never written as ``{name}``: that is the notation of a path parameter (``/orders/{id}``), so an
agent or a route↔call matcher would read it as a segment the caller fills in.

Inbound detectors set the suffix where they find the prefix unknown. Outbound URLs are rendered
by shared code (``statements_common``) with each runtime part as ``{name}``;
:func:`apply_outbound` turns a URL that *starts* with such a part into the suffix form, for
every language, once the file is parsed.
"""

from __future__ import annotations

from ..schemas import FileRecord

#: Outbound records whose ``endpoint`` is a URL.
_OUTBOUND = frozenset({"api_call"})

#: Stands for a runtime prefix inside a composed base, for detectors that build an address by
#: concatenating wiring parts: ``"\0" + "/v1"`` is "an unknown prefix, then ``/v1``".
RUNTIME_MARK = "\0"


def marked_address(base: str, own: str | None) -> tuple[str | None, str | None]:
    """``(endpoint, endpointSuffix)`` of a route served under ``base`` (which may hold
    :data:`RUNTIME_MARK`) at its own path ``own``, joined by plain concatenation. A marked base
    gives no endpoint and the text after the last mark plus ``own`` as the suffix."""
    if own is None:
        return None, None
    if RUNTIME_MARK not in base:
        return (base + own) or None, None
    tail = base.rsplit(RUNTIME_MARK, 1)[1] + own
    return None, (tail if tail.startswith("/") and tail.strip("/") else None)


def runtime_suffix(rendered: str | None) -> str | None:
    """The known tail of a rendered address that starts with a runtime part (``{name}``):
    ``{contextRoot}/orders/{id}`` → ``/orders/{id}``; ``{host}:{port}/x`` → ``/x``;
    ``{scheme}://{host}/x`` → ``/x``. None when the address does not start with a runtime part,
    or has no path after it (``{base}``, ``{base}{path}``, ``{base}/``)."""
    if not rendered or not rendered.startswith("{"):
        return None
    start = 0
    scheme = rendered.find("://")
    if scheme != -1:
        start = scheme + 3
    slash = rendered.find("/", start)
    if slash == -1:
        return None
    tail = rendered[slash:]
    return tail if tail.strip("/") else None


def join_suffix(*parts: str | None) -> str | None:
    """Known path parts after an unknown prefix, joined with single slashes: ``("/api", "x")``
    → ``/api/x``. None when no part has text (a suffix of just ``/`` says nothing)."""
    segments = [seg for part in parts if part for seg in part.split("/") if seg]
    return "/" + "/".join(segments) if segments else None


def apply_outbound(record: FileRecord) -> None:
    """Every outbound URL of ``record`` that starts with a runtime part: ``endpoint`` cleared,
    the known tail in ``endpointSuffix``. Any other address is left as it is."""
    for stmt in record.statements:
        if stmt.semanticType in _OUTBOUND and stmt.endpoint and stmt.endpoint.startswith("{"):
            stmt.endpointSuffix = runtime_suffix(stmt.endpoint)
            stmt.endpoint = None
