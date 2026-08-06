"""Angular ``uiRole`` marking — the honest, decorator-keyed component marker.

Angular declares UI entities with a class decorator: ``@Component`` (a component),
``@Directive`` (a structural/attribute directive), ``@Pipe`` (a template transform). The
base parser already captures these in ``Class.decorators`` (the name stripped to its last
segment, so ``@Component`` → ``"Component"``), so marking is a pure post-``extract`` read of
that list — no new node, no tree walk. The signal is exact: a decorator is either present or
it is not, so there are no false positives.

``@Injectable`` is a *service*, not a UI role, and is deliberately not marked (no ``service``
role exists yet). A class with none of these decorators stays unmarked."""

from __future__ import annotations

from ...schemas import FileRecord

# Angular class decorator name → uiRole. Keyed on the decorator name as captured
# (last ``.``-segment, un-prefixed), matching ``Class.decorators[].name``.
_DECORATOR_ROLES = {
    "Component": "component",
    "Directive": "directive",
    "Pipe": "pipe",
}


def mark_angular_ui_roles(record: FileRecord) -> None:
    """Set ``uiRole`` on ``@Component`` / ``@Directive`` / ``@Pipe`` classes (see module docstring)."""
    for cls in record.classes:
        if cls.uiRole is not None:
            continue
        for dec in cls.decorators:
            role = _DECORATOR_ROLES.get(dec.name)
            if role is not None:
                cls.uiRole = role
                break
