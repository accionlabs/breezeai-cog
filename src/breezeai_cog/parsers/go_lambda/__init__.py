"""AWS Lambda handler detection for Go (``github.com/aws/aws-lambda-go``). A registered additive
detector (``handlers.py``) — it owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
