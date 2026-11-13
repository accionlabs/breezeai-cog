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

#: Matched in exact case: the tools read these names as written, and a lower-case
#: `docs/agents.md` or `docs/providers/claude.md` is prose about an agent, not its instructions.
_AGENT_INSTRUCTION_NAMES = frozenset({"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md"})
#: File stems in a decisions folder that are its index or template, not a decision.
_ADR_NON_RECORD_STEMS = frozenset({"readme", "index", "_index", "template"})
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
    plugin_root: Callable[[str], bool],
) -> str:
    """Return the semantic type of the Markdown file at repo-relative ``path``.

    ``plugin_root(dir)`` tells whether a repo-relative directory is a Claude Code or Cursor
    plugin root (it holds ``.claude-plugin/plugin.json`` or ``.cursor-plugin/plugin.json``);
    a plugin's ``commands/`` and ``agents/`` folders sit directly in it."""
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
            or _plugin_folder(dirs, "agents", plugin_root)
        )
    ):
        return "agent"

    # Copilot prompt files are slash commands only in `.github/prompts/`; elsewhere `*.prompt.md`
    # is commonly a prompt template a skill loads (seen in breezeai-claude-plugin references/).
    if upper.endswith(".PROMPT.MD") and _contains(dirs, (".github", "prompts")):
        return "command"
    # Cursor commands are plain Markdown (no front matter) in `.cursor/commands/`.
    if _contains(dirs, (".cursor", "commands")) and name.lower().endswith(_MARKDOWN_SUFFIXES):
        return "command"
    if "commands" in dirs and name.lower().endswith(_MARKDOWN_SUFFIXES):
        # A generic `commands/` folder is often CLI documentation (and `description` is common
        # doc front matter): only a Claude command folder, a plugin's, or a file declaring a
        # command key is a slash command.
        if (
            ".claude" in dirs
            or _has_any(front_matter, _COMMAND_KEYS)
            or _plugin_folder(dirs, "commands", plugin_root)
        ):
            return "command"

    if (
        name in _AGENT_INSTRUCTION_NAMES
        or path.endswith(".github/copilot-instructions.md")
        or name == ".cursorrules"
        # `.mdc` is Cursor's rule format and nothing else: a rule wherever it sits (a Cursor
        # plugin keeps them in `rules/`).
        or name.lower().endswith(".mdc")
    ):
        return "agent_instructions"

    stem, _, suffix = upper.partition(".")
    if stem == "README" and suffix in ("MD", "MDX", "RST"):
        return "readme"
    if stem in _CHANGELOG_NAMES and suffix in ("MD", "MDX"):
        return "changelog"

    if _is_adr(name, dirs, headings):
        return "adr"

    return "doc"


def _is_adr(name: str, dirs: tuple[str, ...], headings: list[dict[str, Any]]) -> bool:
    """A decision record: any file in ``docs/adr/`` or ``docs/decisions/`` but its index or
    template; in another ``adr/`` folder, only one named ``NNNN-…`` or with ``## Status`` and
    ``## Decision`` headings; anywhere else, one with both the name and the headings."""
    stem = name.rsplit(".", 1)[0].lower()
    if stem in _ADR_NON_RECORD_STEMS or "template" in stem:
        return False
    numbered = _ADR_NAME.match(name) is not None
    if _contains(dirs, ("docs", "adr")) or _contains(dirs, ("docs", "decisions")):
        return True
    if "adr" in dirs:
        return numbered or _has_adr_headings(headings)
    return numbered and _has_adr_headings(headings)


def _has_any(front_matter: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(k in front_matter for k in keys)


def _plugin_folder(dirs: tuple[str, ...], name: str, plugin_root: Callable[[str], bool]) -> bool:
    """True when a ``name`` folder in ``dirs`` sits directly in a plugin root. A ``commands/``
    deeper in a plugin (``docs/commands/``, a skill's ``references/commands/``) is documentation,
    not the plugin's commands."""
    return any(part == name and plugin_root("/".join(dirs[:i])) for i, part in enumerate(dirs))


def _contains(parts: tuple[str, ...], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(parts[i : i + n] == seq for i in range(len(parts) - n + 1))


def _has_adr_headings(headings: list[dict[str, Any]]) -> bool:
    level2 = {str(h.get("text", "")).strip().lower() for h in headings if h.get("level") == 2}
    return "status" in level2 and "decision" in level2
