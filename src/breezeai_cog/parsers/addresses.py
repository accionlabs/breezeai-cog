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

An address keeps every part, even when none is literal (``${path}``, ``${host}${path}``,
``${config:api-verticle.address}``): it then names where the address comes from, not the address.
It is empty only when no part has a name (``${value}``, ``{param}``): that says nothing.

What is inside a marker names the value's source (:func:`runtime_name`), the same way for both
markers, so two values never share a name only because their last parts match:

- ``config:a.b`` — a configuration entry read by literal keys
  (``config().getJsonObject("a").getString("b")``, ``env.getProperty("a.b")``), the key path whole;
- ``env:X`` — an environment variable (``process.env.X``, ``os.getenv("X")``,
  ``System.getenv("X")``);
- a name or member path, written whole less a leading ``this.`` / ``self.``
  (``config.contextRoot`` → ``${config.contextRoot}``, ``user.id`` → ``{user.id}``);
- another lookup by a literal key: the key; another call: the called function;
- anything else: ``${value}`` (``{param}`` for a request value).
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
#: A name or member path of plain identifiers (``user.id``), spaces removed.
_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
#: Receivers that only say "this object": left out of a value's name.
_SELF = ("this.", "self.")
_KEYED = re.compile(r"""\(\s*["'`]([\w.\-]+)["'`]\s*[,)]""")
_CALLED = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
#: A name that already carries its source kind (``config:a.b``, ``env:X``).
_KINDED = re.compile(r"(?:config|env):[^{}\s]+")
_STR = r"""["'`]([^"'`]+)["'`]"""
#: The rest of a lookup after its key: a default argument, the closing bracket.
_TAIL = r"""\s*(?:,[^()]*)?[)\]]"""
#: An environment variable read by name.
_ENV = re.compile(
    r"(?:process\.env\.([A-Za-z_]\w*)"
    rf"|process\.env\[\s*{_STR}\s*\]"
    rf"|(?:[\w.]+\.)?(?:getenv|Getenv|GetEnvironmentVariable)\s*\(\s*{_STR}{_TAIL}"
    rf"|os\.environ(?:\.get\s*\(|\s*\[)\s*{_STR}{_TAIL}"
    rf"|ENV(?:\.fetch\s*\(|\s*\[)\s*{_STR}{_TAIL})")
#: A configuration object (``config()``, ``container.config()``, ``this.config``,
#: ``getConfig()``) and the getters that read it by key.
_CONFIG_ROOT = re.compile(
    r"(?:[A-Za-z_$][\w$]*(?:\(\s*\))?\s*\.\s*)*"
    r"(?:config|getConfig|configuration|conf)(?:\(\s*\))?")
_CONFIG_GET = re.compile(
    r"\s*\.\s*(?:get|getString|getObject|getJsonObject|getJsonArray|getValue|getInteger"
    rf"|getLong|getBoolean|getDouble|getProperty)\s*\(\s*{_STR}\s*(?:,[^()]*)?\)")
#: Spring's ``Environment`` read by key: ``env.getProperty("a.b")``.
_PROPERTY = re.compile(rf"(?:this\.)?(?:env|environment)\s*\.\s*getProperty\s*\(\s*{_STR}\s*(?:,[^()]*)?\)")


def source_kind(expr_text: str) -> str | None:
    """``env:X`` / ``config:a.b`` when ``expr_text`` is exactly an environment variable or a
    configuration entry read by literal keys, else None."""
    text = expr_text.strip()
    if _KINDED.fullmatch(text):
        return text
    env = _ENV.fullmatch(text)
    if env is not None:
        return "env:" + next(g for g in env.groups() if g)
    prop = _PROPERTY.fullmatch(text)
    if prop is not None:
        return "config:" + prop.group(1)
    root = _CONFIG_ROOT.match(text)
    if root is None:
        return None
    keys: list[str] = []
    at = root.end()
    while at < len(text):
        got = _CONFIG_GET.match(text, at)
        if got is None:
            return None
        keys.append(got.group(1))
        at = got.end()
    return "config:" + ".".join(keys) if keys else None


def _drop_self(path: str) -> str:
    for prefix in _SELF:
        if path.startswith(prefix) and len(path) > len(prefix):
            return path[len(prefix):]
    return path


def value_path(expr_text: str) -> str | None:
    """The name of a value an address is built from, when its source text is a name or member
    path of plain identifiers: the whole path, spaces removed, less a leading ``this.`` /
    ``self.`` (``user.id`` → ``user.id``, ``this.baseUrl`` → ``baseUrl``). None otherwise (a
    call, an index, an operator)."""
    text = "".join(expr_text.split())
    return _drop_self(text) if _DOTTED.fullmatch(text) else None


def runtime_name(expr_text: str) -> str:
    """The name of a value read at runtime, from its source text: a configuration entry or an
    environment variable read by literal keys carries its kind (:func:`source_kind`:
    ``config:api-verticle.address``, ``env:BASE_PATH``); a name or member path is
    written whole (:func:`value_path`; ``config.contextRoot`` → ``config.contextRoot``,
    ``this.prefix`` → ``prefix``); a lookup by a literal key gives the key
    (``config().getString("pathPrefix")``, ``os.getenv("BASE")`` → ``pathPrefix``, ``BASE``);
    another call gives the called name (``prefixFromEnv()`` → ``prefixFromEnv``); anything
    else is ``value``."""
    text = expr_text.strip()
    kind = source_kind(text)
    if kind is not None:
        return kind
    path = value_path(text)
    if path is not None:
        return path
    if _SIMPLE.fullmatch(text):  # a JavaScript name with a `$` in it
        return _drop_self("".join(text.split()))
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


#: Names a value gets when nothing names it.
_UNNAMED = frozenset({"value", "param", ""})
_ANY_MARKER = re.compile(r"\$?\{([^{}]*)\}")


def informative(address: str | None) -> str | None:
    """``address``, or None when it says nothing: no literal part besides separators and no
    marker with a name (``${value}``, ``${value}/{param}``). An address of named markers only
    (``${path}``, ``${config:api-verticle.address}``) is kept: it names where the address
    comes from."""
    if address is None:
        return None
    names = _ANY_MARKER.findall(address)
    if not names:
        return address
    rest = _PLACEHOLDER.sub("", _RUNTIME_PART.sub("", address))
    if rest.strip("/:.?&=# "):
        return address
    return address if any(n not in _UNNAMED for n in names) else None


def has_literal(address: str | None) -> bool:
    """Whether an address has a literal part besides its markers and separators: the test a
    detector uses to tell a path argument from another value (``app.use(mw)`` is no mount),
    never whether to keep the address (:func:`informative`)."""
    if address is None:
        return False
    rest = _PLACEHOLDER.sub("", _RUNTIME_PART.sub("", address))
    return bool(rest.strip("/:.?&=# "))


def mark_runtime(rendered: str) -> str:
    """Every ``{name}`` placeholder of a rendered path as ``${name}``: for a path whose
    placeholders all stand for values read at runtime (a route path or a mount prefix, where
    no route parameter is ever rendered as ``{name}``)."""
    return _PLACEHOLDER.sub(lambda m: runtime_part(m.group(1)), rendered)


def inbound_template(rendered: str | None) -> str | None:
    """A rendered route path with its placeholders marked (:func:`mark_runtime`); None when it
    says nothing (:func:`informative`)."""
    return informative(mark_runtime(rendered)) if rendered is not None else None


def join_runtime(base: str, path: str) -> str:
    """A base holding a runtime part joined with a path by one ``/`` (``${prefix}`` +
    ``/x`` → ``${prefix}/x``): never a ``/`` put in front of the runtime part."""
    return base.rstrip("/") + "/" + path.lstrip("/") if path.strip("/") else base


def outbound_template(url: str | None) -> str | None:
    """A rendered outbound URL: each ``{name}`` in its base — up to the first ``/`` of the path,
    the scheme and host included — becomes ``${name}``; the ones after it stay ``{name}``
    (request values) — except a configuration entry or an environment variable, which is never
    a request value (``{config:v}`` → ``${config:v}`` anywhere). None when it says nothing
    (:func:`informative`)."""
    if not url:
        return url
    start = url.find("://")
    start = start + 3 if start != -1 else 0
    slash = url.find("/", start)
    end = len(url) if slash == -1 else slash
    base = _PLACEHOLDER.sub(lambda m: runtime_part(m.group(1)), url[:end])
    rest = _PLACEHOLDER.sub(
        lambda m: runtime_part(m.group(1)) if _KINDED.fullmatch(m.group(1)) else m.group(0),
        url[end:])
    return informative(base + rest)


def normalize_addresses(record: FileRecord) -> None:
    """Once per file, for every language: an outbound URL with a runtime part in its base gets
    that part marked ``${name}`` (:func:`outbound_template`); a route address that starts with
    a runtime part loses the ``/`` a path join put in front of it (``/${base}/x`` →
    ``${base}/x``: the value may hold its own ``/``, a scheme or a host). An address that says
    nothing (:func:`informative`) is cleared."""
    for stmt in record.statements:
        endpoint = stmt.endpoint
        if not endpoint or "{" not in endpoint:
            continue
        if stmt.semanticType in _OUTBOUND:
            stmt.endpoint = outbound_template(endpoint)
        elif stmt.semanticType == "route" and RUNTIME_OPEN in endpoint:
            if endpoint.startswith("/" + RUNTIME_OPEN):
                endpoint = endpoint[1:]
            stmt.endpoint = informative(endpoint)
