"""Spring Boot module configuration as registered facts on the Java index.

* ``spring.context-paths`` — module root (``""`` = repo root) → its servlet context path, the
  prefix of every HTTP route in that module (``server.servlet.context-path`` and friends).
  None = set in config but unresolvable (the base files disagree, or a ``${…}`` placeholder).
  A module without one is absent.
* ``spring.properties`` — module root → its base-config properties (``key → value``; None =
  the base files disagree), which resolve ``${…}`` placeholders in annotation values such as a
  ``@KafkaListener`` topic.

A module is the directory above a Java file's ``src/main/java``; its configuration is read once
per module from ``src/main/resources`` (see :mod:`..java.config`)::

    orders/src/main/java/com/x/OrderController.java    → module "orders"
    orders/src/main/resources/application.yml          → server.servlet.context-path: /orders
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..java.config import module_of, spring_context_paths, spring_properties

CONTEXT_PATHS = "spring.context-paths"
PROPERTIES = "spring.properties"


def context_paths(index: Any | None) -> dict[str, str | None]:
    return index_fact(index, CONTEXT_PATHS) or {}


def module_properties(index: Any | None) -> dict[str, dict[str, str | None]]:
    return index_fact(index, PROPERTIES) or {}


def _collect(root: Node | None, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, str] | None:
    """(repo root, this file) when the file is in a module (under ``src/main/java``)."""
    if repo_root is None or module_of(rel) is None:
        return None
    return str(repo_root), rel


def _by_repo(values: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for repo, rel in values:
        out.setdefault(repo, []).append(rel)
    return out


def _resolve_context_paths(values: list[tuple[str, str]], index: Any) -> dict[str, str | None] | None:
    out: dict[str, str | None] = {}
    for repo, rels in _by_repo(values).items():
        out.update(spring_context_paths(Path(repo), rels))
    return out or None


def _resolve_properties(
    values: list[tuple[str, str]], index: Any,
) -> dict[str, dict[str, str | None]] | None:
    out: dict[str, dict[str, str | None]] = {}
    for repo, rels in _by_repo(values).items():
        out.update(spring_properties(Path(repo), rels))
    return out or None


# Every file: where a file lives decides its module, not what it says.
register_fact(FactSpec(
    name=CONTEXT_PATHS, language="java", gate=(b"",),
    collect=_collect, resolve=_resolve_context_paths,
))
register_fact(FactSpec(
    name=PROPERTIES, language="java", gate=(b"",),
    collect=_collect, resolve=_resolve_properties,
))
