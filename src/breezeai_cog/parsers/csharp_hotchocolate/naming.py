"""Resolver method name → GraphQL field name.

HotChocolate does not expose a resolver under its C# method name. ``DefaultNamingConventions``
strips a leading ``Get`` and a trailing ``Async``, then lower-cases the first character — so
``GetBookByIdAsync`` is served as ``bookById``, and that is the name a client sends. This is
framework code applied to every resolver, not a team habit, so the computed name is a fact
about the program. ``[GraphQLName("x")]`` overrides it.

The C# method name is kept separately on the statement's ``handler``, so nothing is lost.
"""

from __future__ import annotations

from ...schemas import Decorator
from ..csharp_aspnet.routes import simple_attr_name
from .mappings import NAME_ATTR

_GET = "Get"
_ASYNC = "Async"


def _override(decorators: list[Decorator]) -> str | None:
    """The explicit ``[GraphQLName("x")]`` value, else None."""
    for dec in decorators:
        if simple_attr_name(dec.name) == NAME_ATTR and dec.args:
            name = dec.args[0].strip().strip('"')
            if name:
                return name
    return None


def _camel(name: str) -> str:
    """Lower-case the first character, as ``DefaultNamingConventions`` does."""
    return name[:1].lower() + name[1:] if name else name


def field_name(method_name: str, decorators: list[Decorator]) -> str:
    """The GraphQL field name HotChocolate exposes for ``method_name``.

    The affix strips are guarded on length so a method named exactly ``Get`` or ``Async``
    keeps its name rather than collapsing to an empty field name.
    """
    override = _override(decorators)
    if override is not None:
        return override
    name = method_name
    if name.startswith(_GET) and len(name) > len(_GET):
        name = name[len(_GET):]
    if name.endswith(_ASYNC) and len(name) > len(_ASYNC):
        name = name[: -len(_ASYNC)]
    return _camel(name)
