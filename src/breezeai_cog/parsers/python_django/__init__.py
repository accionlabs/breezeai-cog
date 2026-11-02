"""Django parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over the base
Python parser via ``claims`` (a Django import). ``views.py`` and ``urlconf.py`` register the
``django.views`` and ``django.urlconf`` facts."""

from __future__ import annotations

from .parser import DjangoParser

PARSERS = [DjangoParser()]
