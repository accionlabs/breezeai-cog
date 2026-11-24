"""Django and Django REST framework parsers. Expose ``PARSERS`` for ``discover_builtin``; selected per-file over the base
Python parser via ``claims`` (a Django import). ``views.py`` and ``urlconf.py`` register the
``django.views`` and ``django.urlconf`` facts; ``routers.py`` the additive ``drf-routers`` detector."""

from __future__ import annotations

from .parser import DjangoParser, DRFParser

PARSERS = [DjangoParser(), DRFParser()]
