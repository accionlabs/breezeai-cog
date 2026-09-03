"""In-house (local-decorator) TypeScript code-first GraphQL framework parser."""

from .parser import GraphQLCodeFirstParser

PARSERS = [GraphQLCodeFirstParser()]
