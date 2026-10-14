"""Security annotations that guard a route (contract role: Guard).

Shared by the Spring and JAX-RS detectors: a class-level annotation guards every handler of the
class, a method-level one only that handler. Each guard is written as its annotation without the
``@`` (``PreAuthorize("hasRole('ADMIN')")``, ``RolesAllowed("admin")``) so the role expression is
kept. ``@PermitAll`` (opens access) and ``@PostAuthorize`` (runs after the handler) are not
guards.
"""

from __future__ import annotations

from ...schemas import Decorator

#: Annotations checked before the handler runs.
GUARD_ANNOTATIONS = frozenset({"PreAuthorize", "Secured", "RolesAllowed", "DenyAll"})


def security_guards(*scopes: list[Decorator]) -> list[str] | None:
    """Guard names from the given decorator lists, outermost scope first (class, then method).
    None when there are none, so the field stays unset."""
    out = [
        f"{d.name}({', '.join(d.args)})" if d.args else d.name
        for decorators in scopes
        for d in decorators
        if d.name in GUARD_ANNOTATIONS
    ]
    return out or None
