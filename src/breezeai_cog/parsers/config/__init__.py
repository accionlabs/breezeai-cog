"""Config-file analyzer. Emits type="config" FileRecords with parsed ``metadata``."""

from .parser import ConfigParser

PARSERS = [ConfigParser()]
