"""Markdown documents — the single owner of ``.md`` / ``.mdc`` (plus ``README.rst`` and
``.cursorrules``). One File + one whole-file ``synthetic`` Statement per file, typed skill /
agent / command / agent_instructions / doc, so the AI capabilities and docs a repo carries are
listable by ``semanticType``. ``.mdx`` is owned by the MDX parser, which reads its prose
through :func:`.parser.read_document`."""

from .parser import MarkdownParser

PARSERS = [MarkdownParser()]
