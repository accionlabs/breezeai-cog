"""Semantic type of a Markdown document, from its name, location and front matter.

Precedence: ``skill`` > ``agent`` > ``command`` > ``agent_instructions`` > ``readme`` >
``changelog`` > ``adr`` > ``doc``. A type that needs front matter (``skill``, ``agent``, and a
``command`` outside a Claude / plugin folder) is never given on a guess: without the fields,
the file is the next type that matches, ending at ``doc`` ("wrong is worse than missing").
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from .frontmatter import string_field

#: The typed documents an AI agent can use, or that record a decision. A folder whose only
#: files are untyped docs (a README, a changelog, prose) yields no ontology; one with any of
#: these does.
AGENT_DOCUMENT_KINDS: frozenset[str] = frozenset(
    {"skill", "agent", "command", "agent_instructions", "adr"}
)

_AGENT_INSTRUCTION_NAMES = frozenset({"CLAUDE.MD", "CLAUDE.LOCAL.MD", "AGENTS.MD", "GEMINI.MD"})
_CHANGELOG_NAMES = frozenset({"CHANGELOG", "HISTORY", "RELEASES"})
_MARKDOWN_SUFFIXES = (".md", ".mdx")
_ADR_NAME = re.compile(r"^\d{4}-.+\.mdx?$", re.IGNORECASE)
#: Front-matter keys only a Claude Code slash command / subagent file has.
_COMMAND_KEYS = ("argument-hint", "allowed-tools", "model", "disable-model-invocation")
_AGENT_KEYS = ("tools", "model", "color")


def classify(
    path: str,
    front_matter: dict[str, Any],
    headings: list[dict[str, Any]],
    in_plugin: Callable[[str], bool],
) -> str:
    """Return the semantic type of the Markdown file at repo-relative ``path``.

    ``in_plugin(dir)`` tells whether a repo-relative directory is inside a Claude Code plugin
    (an ancestor holds ``.claude-plugin/plugin.json``); consulted for ``commands/`` and ``agents/``."""
    p = PurePosixPath(path)
    name = p.name
    upper = name.upper()
    dirs = p.parts[:-1]
    has_name = string_field(front_matter, "name") is not None
    has_description = string_field(front_matter, "description") is not None

    if name == "SKILL.md" and has_name and has_description:
        return "skill"

    if upper.endswith(".AGENT.MD") and (has_description or _contains(dirs, (".github", "agents"))):
        return "agent"
    if (
        "agents" in dirs
        and name.lower().endswith(_MARKDOWN_SUFFIXES)
        and has_name
        and has_description
        # `name` + `description` are also generic doc front matter (Docusaurus, VitePress): an
        # `agents/` folder outside Claude / a plugin also needs an agent key.
        and (
            ".claude" in dirs
            or _has_any(front_matter, _AGENT_KEYS)
            or in_plugin(_folder(dirs, "agents"))
        )
    ):
        return "agent"

    # Copilot prompt files are slash commands only in `.github/prompts/`; elsewhere `*.prompt.md`
    # is commonly a prompt template a skill loads (seen in breezeai-claude-plugin references/).
    if upper.endswith(".PROMPT.MD") and _contains(dirs, (".github", "prompts")):
        return "command"
    if "commands" in dirs and name.lower().endswith(_MARKDOWN_SUFFIXES):
        # A generic `commands/` folder is often CLI documentation (and `description` is common
        # doc front matter): only a Claude command folder, a plugin's, or a file declaring a
        # command key is a slash command.
        if (
            ".claude" in dirs
            or _has_any(front_matter, _COMMAND_KEYS)
            or in_plugin(_folder(dirs, "commands"))
        ):
            return "command"

    if (
        upper in _AGENT_INSTRUCTION_NAMES
        or path.endswith(".github/copilot-instructions.md")
        or name == ".cursorrules"
        or (name.endswith(".mdc") and _contains(dirs, (".cursor", "rules")))
    ):
        return "agent_instructions"

    stem, _, suffix = upper.partition(".")
    if stem == "README" and suffix in ("MD", "MDX", "RST"):
        return "readme"
    if stem in _CHANGELOG_NAMES and suffix in ("MD", "MDX"):
        return "changelog"

    if (
        _contains(dirs, ("docs", "adr"))
        or _contains(dirs, ("docs", "decisions"))
        or "adr" in dirs
        or (_ADR_NAME.match(name) and _has_adr_headings(headings))
    ):
        return "adr"

    return "doc"


def _has_any(front_matter: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(k in front_matter for k in keys)


def _folder(dirs: tuple[str, ...], name: str) -> str:
    """The repo-relative path of the innermost ``name`` folder in ``dirs``."""
    return "/".join(dirs[: len(dirs) - dirs[::-1].index(name)])


def _contains(parts: tuple[str, ...], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(parts[i : i + n] == seq for i in range(len(parts) - n + 1))


def _has_adr_headings(headings: list[dict[str, Any]]) -> bool:
    level2 = {str(h.get("text", "")).strip().lower() for h in headings if h.get("level") == 2}
    return "status" in level2 and "decision" in level2
