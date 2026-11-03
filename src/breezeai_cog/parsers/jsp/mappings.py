"""Capability metadata for the JSP parser.

There is no JSP tree-sitter grammar, so the JSP constructs themselves (directives, EL, JSP
comments) are found by :mod:`.scanner` and carry the names the JSP specification gives them.
Markup is parsed with the ``html`` grammar and scriptlet code with the ``java`` grammar, so
those statements keep their real grammar node types.
"""

from __future__ import annotations

from ..java.mappings import COMMENT_TYPES as JAVA_COMMENT_TYPES
from ..java.mappings import STATEMENT_TYPES as JAVA_STATEMENT_TYPES

#: Page files (standard and XML syntax), fragments, and tag files.
EXTENSIONS: tuple[str, ...] = (".jsp", ".jspx", ".jspf", ".tag", ".tagx")

#: Tag files are reusable components; everything else is a view.
TAG_FILE_EXTENSIONS: tuple[str, ...] = (".tag", ".tagx")

#: Construct names from the JSP specification (found by the scanner, not a grammar).
JSP_TYPES: list[str] = ["jsp_directive", "jsp_comment", "el_expression"]

#: ``html`` grammar node types emitted: a directly served page (its root ``document``), prefixed
#: action / custom-tag elements and links, HTML comments.
MARKUP_TYPES: list[str] = ["document", "element", "comment"]

STATEMENT_TYPES: list[str] = sorted(
    {*JSP_TYPES, *MARKUP_TYPES, *JAVA_STATEMENT_TYPES, *JAVA_COMMENT_TYPES}
)

FRAMEWORKS: list[str] = ["jsp"]
