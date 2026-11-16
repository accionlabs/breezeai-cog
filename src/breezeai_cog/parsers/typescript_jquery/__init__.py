"""jQuery AJAX outbound calls (``$.ajax`` / ``$.get`` / ``$.post`` / ``$.getJSON`` / ``$.getScript`` /
``.load``) for TypeScript and JavaScript, and in inline ``<script>`` blocks of markup. A registered
additive detector (``calls.py``); the package owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
