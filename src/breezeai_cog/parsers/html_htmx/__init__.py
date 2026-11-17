"""htmx requests (``hx-get`` / ``hx-post`` / ``hx-put`` / ``hx-patch`` / ``hx-delete``, and their
``data-hx-*`` spelling) in HTML markup, as ``api_call`` statements (``calls.py``). The package owns
no file: the markup parser calls :func:`~.calls.detect_htmx_calls`."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
