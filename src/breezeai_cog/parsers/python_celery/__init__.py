"""Celery for Python. A registered additive detector (``tasks.py``) and the task names it reads
from other files, a registered fact (``task_names.py``); the package owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
