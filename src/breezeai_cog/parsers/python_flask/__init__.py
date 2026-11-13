"""Flask detection — an additive detector, not a parser: a blueprint's routes often sit in a file
that never imports Flask (``from app.auth import bp`` then ``@bp.route(...)``), so no ``claims``
check could own them. ``routes.py`` registers the ``flask`` detector and ``wiring.py`` the
``flask.wiring`` fact (blueprints and their registrations across files)."""

from __future__ import annotations

PARSERS: list[object] = []
