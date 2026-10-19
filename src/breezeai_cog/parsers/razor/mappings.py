"""Capability metadata for the Razor (``.cshtml`` / ``.razor``) parser.

``STATEMENT_TYPES`` are the real ``razor`` grammar node types this parser emits as
``Statement.nodeType``. Unlike ``.aspx`` (no grammar → shadow-source), the ``razor`` grammar
parses markup **and** the embedded C# in one pass, so every record keeps its genuine grammar
node type — discovered empirically by dumping the grammar over real ``.cshtml``/``.razor``.
"""

from __future__ import annotations

#: ``razor`` grammar node types emitted as Statement.nodeType (template side).
STATEMENT_TYPES: list[str] = [
    "razor_page_directive",
    "razor_model_directive",
    "razor_implicit_expression",
    "razor_html_attribute",
    "razor_foreach",
    "razor_if",
]

#: Framework this parser reports. Covers Razor views (``.cshtml``) and Blazor components
#: (``.razor``) — one markup dialect. (New framework value; document in the spec.)
FRAMEWORKS: list[str] = ["razor"]
