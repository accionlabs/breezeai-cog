"""MarkdownParser — one File + one whole-file Statement per Markdown document.

Every ``.md`` / ``.mdx`` / ``.mdc`` file (plus ``README.rst`` and ``.cursorrules``) becomes a
``type="config"`` FileRecord — so it never counts as an analyzed language — whose ``metadata``
holds ``{kind, category: "documentation", frontmatter, title, headings}``, and, under statement
capture, one Statement:

* ``nodeType="document"``, ``semanticType`` = the kind (see :mod:`.classify`)
* ``text`` = the whole file, secrets masked, cut at 64 KB on a line boundary with the loss
  marked inline (``metadata.truncated`` / ``originalSize`` on the File)
* ``name`` / ``description`` from the front matter (name falls back to the title, the command
  file name, then the file name)

Files over 512 KB keep the File record only. Like ``structured_data``, the statement is not
embedded: it is found by exact filters (``semanticType``, ``name``, ``text`` contains).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ...emit import file_id, statement_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..structured_json.redaction import redact_text
from .classify import classify
from .frontmatter import _secret_key, load_front_matter, split_front_matter, string_field

#: Statement text cap, in UTF-8 bytes (the largest SKILL.md measured is 60.5 KB).
MAX_TEXT_BYTES = 64 * 1024
#: Above this size a document keeps its File record (metadata + outline) and no Statement.
MAX_STATEMENT_FILE_BYTES = 512 * 1024
_MAX_HEADINGS = 200
_MAX_HEADING_LEVEL = 3
_MAX_HEADING_CHARS = 200

# Linear: the closing ``#`` sequence is stripped in code, not by a backtracking regex tail.
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
#: Inline base64 payloads (pasted images) carry no meaning for a reader and would eat the cap.
_DATA_URI = re.compile(r"data:([a-zA-Z0-9.+/\-]+);base64,[A-Za-z0-9+/=]{200,}")
#: Env-style assignment of a variable (``export DB_PASSWORD=…``); masked when the name is
#: secret-like (:data:`_SECRET_NAME`). Upper-case names only, so prose ("the password = …") is
#: untouched. The name is matched possessively and checked in code, so a long line cannot
#: backtrack quadratically.
_ENV_ASSIGN = re.compile(
    r"(?m)^([ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*+[ \t]*[=:][ \t]*)([\"']?)[^\s\"']+\2"
)
_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY")
#: A ``key:`` line in YAML front matter (optionally a list item).
_FM_KEY = re.compile(r"^([ \t]*(?:-[ \t]+)?)([\w.\-]+|\"[^\"]*\"|'[^']*')([ \t]*:)(.*)$")


class MarkdownParser(BaseParser):
    name = "markdown"
    schema_version = SCHEMA_VERSION
    extensions = (".md", ".mdx", ".mdc", "README.rst", ".cursorrules")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        size = len(ctx.source)
        text = ctx.source.decode("utf-8", "replace").lstrip("﻿")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        rst = ctx.path.endswith(".rst")

        fm_text, body_start = (None, 0) if rst else split_front_matter(lines)
        front_matter, fm_ok = load_front_matter(fm_text)
        headings = _rst_headings(lines) if rst else _atx_headings(lines, body_start)
        title = _redacted(string_field(front_matter, "title")) or next(
            (h["text"] for h in headings if h["level"] == 1), None
        )
        kind = classify(ctx.path, front_matter, headings, _plugin_check(ctx.repo_root))

        metadata: dict[str, Any] = {
            "kind": kind,
            "category": "documentation",
            "frontmatter": front_matter,
            "title": title,
            "headings": headings,
        }
        if not fm_ok:
            metadata["frontmatterError"] = True

        record = FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",
            language="rst" if rst else "markdown",
            loc=count_loc(text),
            metadata=metadata,
        )

        # FileRecord copies ``metadata`` on validation: later flags go on the record's own dict.
        assert record.metadata is not None
        if size > MAX_STATEMENT_FILE_BYTES:
            record.metadata.update(truncated=True, originalSize=size)
            return record
        if not ctx.capture_statements:
            return record

        # Secret-named front-matter values are masked in the text too, not only in metadata.
        if body_start:
            text = "\n".join(_mask_front_matter(lines, body_start))
        body, end_line, dropped = _cap(_clean(text), lines)
        if dropped:
            record.metadata.update(truncated=True, originalSize=size)
        stem = PurePosixPath(ctx.path).name.split(".")[0]
        record.statements.append(
            Statement(
                id=statement_id(ctx.path, 1, 0),
                parentId=record.id,
                nodeType="document",
                semanticType=kind,  # type: ignore[arg-type]
                text=body,
                startLine=1,
                endLine=end_line,
                path=ctx.path,
                name=_redacted(string_field(front_matter, "name"))
                or (stem if kind == "command" else None)
                or title
                or PurePosixPath(ctx.path).name,
                description=_redacted(string_field(front_matter, "description")),
            )
        )
        return record


def _clean(text: str) -> str:
    text = redact_text(text)
    text = _ENV_ASSIGN.sub(_mask_env, text)
    return _DATA_URI.sub(lambda m: f"data:{m.group(1)};base64,…", text)


def _mask_env(m: re.Match[str]) -> str:
    if not _SECRET_NAME.search(m.group(1)):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}***{m.group(2)}"


def _redacted(value: str | None) -> str | None:
    return redact_text(value) if value else value


def _mask_front_matter(lines: list[str], body_start: int) -> list[str]:
    """``lines`` with the values of secret-named front-matter keys (``api_token: x``, and the
    indented block under such a key) replaced by ``***``. Front matter is ``lines[1 :
    body_start - 1]``, between the delimiters."""
    out = list(lines)
    block_indent: int | None = None  # indent of a secret key whose value is a nested block
    for i in range(1, body_start - 1):
        line = out[i]
        indent = len(line) - len(line.lstrip(" \t"))
        if block_indent is not None:
            if line.strip() and indent > block_indent:
                out[i] = line[:indent] + "***"
                continue
            block_indent = None
        m = _FM_KEY.match(line)
        if m is None or not _secret_key(m.group(2).strip("\"'")):
            continue
        if m.group(4).strip():
            out[i] = f"{m.group(1)}{m.group(2)}{m.group(3)} ***"
        else:
            block_indent = indent
    return out


def _cap(text: str, lines: list[str]) -> tuple[str, int, int]:
    """Cut ``text`` to :data:`MAX_TEXT_BYTES` on a line boundary → ``(text, endLine, dropped)``.
    ``dropped`` is the number of bytes left out (0 when nothing was cut); the cut is marked
    inline. A first line longer than the cap is hard-cut."""
    total = len(text.encode("utf-8"))
    last_line = max(len(lines) - (1 if lines and lines[-1] == "" else 0), 1)
    if total <= MAX_TEXT_BYTES:
        return text, last_line, 0
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        size = len(line.encode("utf-8")) + 1
        if used + size > MAX_TEXT_BYTES:
            break
        kept.append(line)
        used += size
    if kept:
        body = "\n".join(kept)
        end_line = len(kept)
    else:  # one unbroken line over the cap
        body = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
        end_line = 1
    dropped = total - len(body.encode("utf-8"))
    return f"{body}\n…[+{dropped} bytes dropped: markdown 64 KB cap]", end_line, dropped


def _atx_headings(lines: list[str], start: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fence: str | None = None
    for i in range(start, len(lines)):
        line = lines[i]
        m = _FENCE.match(line)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        h = _ATX.match(line)
        heading = h.group(2).rstrip(" \t#").strip() if h else ""
        if h and heading and len(h.group(1)) <= _MAX_HEADING_LEVEL:
            out.append(
                {
                    "level": len(h.group(1)),
                    "text": _heading_text(heading),
                    "line": i + 1,
                }
            )
            if len(out) >= _MAX_HEADINGS:
                break
    return out


def _heading_text(text: str) -> str:
    # Redact before the cut so a token is not split past its pattern; bound the input first.
    return redact_text(text[: _MAX_HEADING_CHARS * 5])[:_MAX_HEADING_CHARS]


def _rst_headings(lines: list[str]) -> list[dict[str, Any]]:
    """reStructuredText section titles (a text line underlined by a repeated punctuation
    character). Levels follow the order in which underline characters first appear."""
    out: list[dict[str, Any]] = []
    order: list[str] = []
    for i in range(1, len(lines)):
        m = _RST_UNDERLINE.match(lines[i])
        prev = lines[i - 1].strip()
        if not m or not prev or _RST_UNDERLINE.match(prev):
            continue
        if m.group(1) not in order:
            order.append(m.group(1))
        level = order.index(m.group(1)) + 1
        if level <= _MAX_HEADING_LEVEL:
            out.append({"level": level, "text": _heading_text(prev), "line": i})
            if len(out) >= _MAX_HEADINGS:
                break
    return out


def _plugin_check(repo_root: Path | str | None) -> Callable[[str], bool]:
    """``in_plugin(dir)`` for :func:`classify`: True when ``dir`` or an ancestor (within the
    repo) holds ``.claude-plugin/plugin.json``."""

    def in_plugin(rel_dir: str) -> bool:
        if repo_root is None:
            return False
        root = Path(repo_root)
        parts = PurePosixPath(rel_dir).parts
        for n in range(len(parts), -1, -1):
            if (root.joinpath(*parts[:n]) / ".claude-plugin" / "plugin.json").is_file():
                return True
        return False

    return in_plugin
