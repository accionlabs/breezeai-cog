"""Markdown documents — the single owner of ``.md`` / ``.mdc`` (plus ``README.rst`` and
``.cursorrules``); ``.mdx`` is code, owned by the MDX parser. One File + one whole-file ``synthetic`` Statement per
file, typed skill / agent / command / agent_instructions / doc, so the AI capabilities and docs
a repo carries are listable by ``semanticType``."""

from .parser import MarkdownParser

PARSERS = [MarkdownParser()]
