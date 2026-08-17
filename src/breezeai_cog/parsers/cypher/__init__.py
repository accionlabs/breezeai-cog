"""Cypher query parser. Exposes PARSERS for discover_builtin."""

from .parser import CypherParser

PARSERS = [CypherParser()]
