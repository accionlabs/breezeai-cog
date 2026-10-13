"""Retired: Fastify is no longer a one-per-file framework parser.

Fastify route detection is now **additive** because a TS/JS file can legitimately
use Fastify alongside another framework parser. Fastify no longer participates in
one-per-file parser selection. The detector in ``routes.py`` registers with the
additive registry and runs for TypeScript files alongside whichever parser owns
the file, enriching or appending route statements in place.
"""
