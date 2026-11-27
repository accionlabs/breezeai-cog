"""AWS SDK for Python (boto3) detection. A registered additive detector — SDK calls
(``sdk_calls.py``); the package owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
