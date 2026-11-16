"""GraphQL.Client (graphql-dotnet) operation detection for C#. A registered additive detector
(``operations.py``) — it owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
