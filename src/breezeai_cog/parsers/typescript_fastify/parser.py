"""Retired: Fastify is no longer a one-per-file framework parser.

Fastify route detection is now **additive** because a TS/JS file can legitimately
use Fastify alongside another framework parser. Fastify no longer participates in
one-per-file parser selection; ``routes.detect_fastify_routes`` is invoked from
``TypeScriptParser.extract`` for every TS/JS file and enriches the owning parser's
statements in place. See ``routes.py`` and ``typescript/parser.py``.
"""
