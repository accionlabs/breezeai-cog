"""ConfigParser — turns a config file into a ``type="config"`` FileRecord whose
``metadata`` holds the parsed config. It plugs into the normal registry /
selection / pipeline like any language parser (no separate ``analyzeConfigRepo`` side
call as in the JS app), and — because the scanner is hierarchical — it captures config
files **anywhere in the tree**, not just the repo root.

`.env` files are supported (variable **names only**, never values) but are ignored by
default (they're in `default_ignores.txt` as secrets); a repo must opt in via
`.repoinclude` to have them analyzed.
"""

from __future__ import annotations

from pathlib import Path

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .extractors import extract_config


class ConfigParser(BaseParser):
    name = "config"
    schema_version = SCHEMA_VERSION
    # suffixes + exact filenames (the registry matches either); patterns via `matches`
    extensions = (
        ".json", ".yaml", ".yml", ".toml", ".ini", ".xml", ".gradle",
        "Dockerfile", "Makefile", "requirements.txt", "Pipfile",
        ".gitignore", ".dockerignore", "LICENSE", "README.md", "README.rst", ".env",
    )

    def matches(self, path: str | Path) -> bool:
        if super().matches(path):
            return True
        name = Path(path).name  # glob-style config names the registry can't express
        return name.startswith("Dockerfile.") or name.startswith(".env.")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",
            language="config",
            loc=count_loc(text),
            metadata=extract_config(ctx.path, text),
        )
