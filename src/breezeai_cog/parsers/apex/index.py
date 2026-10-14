"""Repo-level pre-pass: lower-cased Apex type name → declaring file.

Apex has no import statements — any class can call any other by name — so cross-file
call resolution needs this map. A name declared in more than one file maps to ``None``
(unresolvable), never to a guessed file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ...utils import repo_relative
from ..treesitter import node_text, parse_source
from .mappings import TYPE_DECLARATIONS

ApexIndex = dict[str, str | None]


def build_class_index(repo_root: Path, files: Sequence[Path]) -> ApexIndex:
    index: ApexIndex = {}
    for file in files:
        if file.suffix != ".cls":
            continue
        try:
            source = file.read_bytes()
            root = parse_source("apex", source).root_node
        except Exception:  # an unreadable or unparsable file just contributes no names
            continue
        rel = repo_relative(file, repo_root)
        for child in root.named_children:
            if child.type not in TYPE_DECLARATIONS:
                continue
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            key = node_text(name_node, source).lower()
            index[key] = rel if key not in index or index[key] == rel else None
    return index
