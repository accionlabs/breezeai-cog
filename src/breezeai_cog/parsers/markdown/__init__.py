"""Markdown documents — the single owner of ``.md`` / ``.mdx`` / ``.mdc``
(plus ``README.rst`` and ``.cursorrules``). One File + one whole-file ``document`` Statement per
file, classified as skill / agent / command / agent_instructions / readme / adr / changelog /
doc, so the AI capabilities and docs a repo carries are listable by ``semanticType``."""

from .parser import MarkdownParser

PARSERS = [MarkdownParser()]
