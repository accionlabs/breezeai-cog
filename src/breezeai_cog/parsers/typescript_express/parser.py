"""Retired: Express is no longer a one-per-file framework parser.

It was displacing (or being displaced by) other TS framework parsers on files that are
legitimately both — e.g. an Angular SSR bootstrap that also runs an ``express()`` server —
losing one side's routes to the selection tie. Express is a substrate (NestJS is built on
it; Angular SSR mounts it), so route detection is now **additive**: ``routes.detect_express``
is invoked from ``TypeScriptParser.extract`` for every TS file, self-guarded on an
``express`` import, enriching the owning parser's statements in place. See ``routes.py`` and
``typescript/parser.py``. The ``express`` framework capability is carried by the base TS
parser's ``FRAMEWORKS`` list.
"""
