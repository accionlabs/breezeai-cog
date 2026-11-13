"""Closed vocabularies from the capture coverage schema (source of truth).

Mirrors the ``enum`` constraints in ``code-capture-coverage-schema.json``. Kept as
``Literal`` aliases so they validate in Pydantic and round-trip into JSON Schema.
"""

from __future__ import annotations

from typing import Literal

# fileRecord.type
FileType = Literal["code", "config"]

# class.type
ClassType = Literal[
    "class",
    "interface",
    "struct",
    "union",
    "record",
    "enum",
    "module",
    "trait",
]

# statement.semanticType — the schema's enum also allows null, expressed in the
# model as ``SemanticType | None``.
SemanticType = Literal[
    "route",
    "api_call",
    "db_method_call",
    "query_statement",
    "eventbus_send",
    "eventbus_publish",
    "eventbus_consumer",
    "verticle_deploy",
    "service_proxy",
    "timer",
    "graphql_entity",
    # data/metadata document (not a behaviour) — the whole content of a captured
    # structured-JSON file, serialized as TOON on the statement `text`. Format-independent
    # by design (see structured_json parser); the serialization is carried on `framework`.
    # The backend persists semanticType verbatim (no enum check) and indexes it, so this
    # value ingests + filters with no backend change; adding it to the Confluence spec's
    # semanticType list (§2.4) is a documentation step, not a functional prerequisite.
    "structured_data",
    # source comment (not a behaviour) — a comment string captured as a first-class
    # Statement (keeping its real structural ``nodeType``: ``comment`` / ``line_comment`` /
    # ``block_comment`` / ``string`` for a Python docstring …) so it is embedded, scoped and
    # searchable. Emitted by the shared comment pass (``parsers/comments_common``) for every
    # language, plus Python docstrings. Persisted verbatim by the backend like
    # ``structured_data`` above.
    "comment",
    # enum member (not a behaviour) — the semantic role of an enum-member declaration, marked
    # on the *same* record while ``nodeType`` keeps its real, per-language grammar type
    # (``enum_assignment`` / ``property_identifier`` / ``enum_member_declaration`` /
    # ``enum_constant`` …). Gives a single cross-language "all enum members" filter that a
    # grammar-coupled ``nodeType`` cannot. Persisted verbatim by the backend (no enum check);
    # adding it to the Confluence spec's §2.4 list is a documentation step.
    "enum_member",
    # data model / entity (not a behaviour) — a declared persistent record type in a schema
    # definition language, e.g. a Prisma ``model`` block (a database table / collection). Marks
    # the *whole* declaration (fields + relations carried on ``text``) so "list every data
    # entity" is one filter, independent of the defining language. The parallel to GraphQL's
    # ``graphql_entity``, kept separate because the vocabulary is IDL-specific; persisted
    # verbatim by the backend (no enum check), so it ingests + filters with no backend change —
    # adding it to the Confluence spec's §2.4 semanticType list is a documentation step.
    "data_model",
    # IaC block types (HCL/Terraform family) — behaviour-only:
    #   resource  = declares/owns infrastructure
    #   data      = reads existing infrastructure owned elsewhere
    #   module    = composition/reuse
    # Structure-only blocks (variable/output/locals/provider/terraform) carry nodeType
    # but NO semanticType, so they are excluded from this enum.
    "iac_resource",
    "iac_data",
    "iac_module",
    # .tfvars attribute assignment (variable values, not variable declarations)
    "iac_variable_value",
    # MCP server tool — an entry point an AI client calls (BREEZEAI-1457). ``endpoint`` = the
    # tool name the client calls (after any rename), ``method="TOOL"``, ``framework`` = the SDK
    # (fastmcp | mcp-python | mcp-sdk-ts | spring-ai-mcp | mcp-java-sdk | mcp-kotlin | mcp-csharp |
    # mcp-go), plus ``description`` and ``params``. Treated like a route by the entry-point contract.
    "mcp_tool",
    # Markdown documents (BREEZEAI-1457) — one whole-file statement per ``.md`` / ``.mdx`` /
    # ``.mdc`` file (``nodeType="document"``, full text up to 64 KB, unembedded), classified by
    # file name / location (parsers/markdown/classify.py):
    #   skill              — ``SKILL.md`` with front-matter name + description (Agent Skills)
    #   agent              — sub-agent definition (``agents/*.md`` / ``.claude/agents``, ``*.agent.md``)
    #   command            — slash command (``.claude/commands``, plugin ``commands/``, ``*.prompt.md``)
    #   agent_instructions — CLAUDE.md / AGENTS.md / GEMINI.md / copilot-instructions / cursor rules
    #   readme | adr | changelog — README, architecture decision record, release history
    #   doc                — any other Markdown
    "skill",
    "agent",
    "command",
    "agent_instructions",
    "readme",
    "adr",
    "changelog",
    "doc",
]
