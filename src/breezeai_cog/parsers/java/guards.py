"""Security annotations that guard a route (contract role: Guard).

Shared by the Spring and JAX-RS detectors: a class-level annotation guards every handler of the
class, a method-level one only that handler. Each guard is written as its annotation without the
``@`` (``PreAuthorize("hasRole('ADMIN')")``, ``RolesAllowed("admin")``) so the role expression is
kept. ``@PostAuthorize`` (runs after the handler) is not a guard.

A method annotation replaces the class annotation of the same family, as the frameworks do: the
Jakarta ``@PermitAll`` / ``@RolesAllowed`` / ``@DenyAll`` family, Spring's ``@PreAuthorize``, and
Spring's ``@Secured``::

    @RolesAllowed("user") class Orders {
        @GET  list()                  → RolesAllowed("user")
        @POST @PermitAll create()     → no guard: open to everyone

``@PermitAll`` and an expression that lets everyone in (``@PreAuthorize("permitAll()")``) are no
guard. A guard asserts ``authRequired`` only when it certainly needs a signed-in caller: a role
check (``@RolesAllowed``, ``@Secured("ROLE_X")``, ``@DenyAll``) or one of Spring Security's own
expressions ``hasRole`` / ``hasAnyRole`` / ``hasAuthority`` / ``hasAnyAuthority`` /
``isAuthenticated`` / ``isFullyAuthenticated``, not combined with ``or`` or a negation. Any other
expression (``@acl.canRead(#id)``, ``isAnonymous()``) is listed but makes no claim.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from ...schemas import Decorator

#: Annotation → the family a method annotation overrides the class one within.
_FAMILIES = {
    "PermitAll": "jakarta", "RolesAllowed": "jakarta", "DenyAll": "jakarta",
    "PreAuthorize": "PreAuthorize", "Secured": "Secured",
}
#: Annotations checked before the handler runs.
GUARD_ANNOTATIONS = frozenset(_FAMILIES) - {"PermitAll"}

#: Spring Security expressions that pass for every caller.
_OPEN = frozenset({"permitAll()", "true"})
#: Spring Security expressions that fail for an anonymous caller.
_SIGNED_IN = re.compile(
    r"\b(?:hasRole|hasAnyRole|hasAuthority|hasAnyAuthority|isAuthenticated|isFullyAuthenticated)\(")
#: What may let an anonymous caller through around such a check.
_ESCAPES = re.compile(r"\bor\b|\|\||!|\bnot\b|\?")


class Guard(NamedTuple):
    #: The annotation as written, without ``@`` (``PreAuthorize(hasRole('ADMIN'))``).
    text: str
    #: Whether it certainly rejects a caller who is not signed in.
    requires_auth: bool


def _expression(d: Decorator) -> str:
    return ", ".join(d.args).strip().strip('"')


def _guard(d: Decorator) -> Guard | None:
    text = f"{d.name}({', '.join(d.args)})" if d.args else d.name
    if d.name == "PermitAll":
        return None
    if d.name == "PreAuthorize":
        expr = _expression(d)
        if expr in _OPEN:
            return None
        return Guard(text, bool(_SIGNED_IN.search(expr)) and not _ESCAPES.search(expr))
    if d.name == "Secured":  # roles; IS_AUTHENTICATED_ANONYMOUSLY lets anyone in
        return Guard(text, "ANONYMOUS" not in _expression(d).upper())
    return Guard(text, True)  # RolesAllowed / DenyAll


def security_guards(class_decorators: list[Decorator],
                    method_decorators: list[Decorator]) -> list[Guard]:
    """The guards of a handler, class scope first: per family, the method's annotations when it
    has any, else the class's."""
    on_method = {_FAMILIES[d.name] for d in method_decorators if d.name in _FAMILIES}
    chosen = [d for d in class_decorators
              if d.name in _FAMILIES and _FAMILIES[d.name] not in on_method]
    chosen += [d for d in method_decorators if d.name in _FAMILIES]
    return [g for d in chosen if (g := _guard(d)) is not None]
