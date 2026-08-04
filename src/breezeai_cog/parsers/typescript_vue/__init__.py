"""Vue parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (a ``.vue`` file, or a ``vue`` /
``vue-router`` import in a ``.ts``/``.js`` file). One parser covers Vue 2 and Vue 3 —
they share the ``.vue`` SFC format and the ``vue-router`` route-array shape; the
version differences (Options vs Composition API, ``new VueRouter`` vs ``createRouter``)
are branches inside detection, not separate parsers."""

from __future__ import annotations

from .parser import VueParser

PARSERS = [VueParser()]
