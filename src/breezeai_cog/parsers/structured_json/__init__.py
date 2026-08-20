"""JSON analyzer — the single owner of ``.json``. Routes named build/config files and
empty/scalar JSON to the shared config extractor (``type="config"``, ``language="config"``);
captures every other non-empty JSON in full as TOON on a ``structured_data`` statement
(``language="structured-json"``), emitted under ``--capture-statements``."""

from .parser import JsonParser

PARSERS = [JsonParser()]
