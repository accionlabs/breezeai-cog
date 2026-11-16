"""tree-sitter parsing helpers."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.treesitter import parse_source


def test_parse_after_a_timeout_starts_clean() -> None:
    # A parse halted by the timeout must not leak into the next parse on the same cached parser:
    # the next tree has to describe the next source only.
    big = b"const a = [" + b"1," * 400_000 + b"];\n"
    with pytest.raises(ValueError):
        parse_source("typescript", big, 1)  # 1 microsecond: always times out
    small = b"function f() { return 1; }\n"
    root = parse_source("typescript", small).root_node
    assert root.end_byte == len(small)
    assert not root.has_error


def test_end_line_is_the_line_of_the_last_character() -> None:
    from breezeai_cog.parsers.treesitter import end_line, line_span

    # VB includes the trailing newline in a block, so the node ends at column 0 of line 4.
    src = b"Public Class A\n    Public Sub Run()\n    End Sub\nEnd Class\n"
    root = parse_source("vb", src).root_node
    block = root.named_children[0].named_children[0]
    assert block.end_point[1] == 0
    assert line_span(block) == (1, 4)
    # A node that ends mid-line is unchanged.
    ts = parse_source("typescript", b"function f() {\n  return 1;\n}\n").root_node.named_children[0]
    assert end_line(ts) == 3


def test_end_line_skips_trailing_blank_lines() -> None:
    from breezeai_cog.parsers.treesitter import line_span

    # The Scala grammar ends an ``=`` bodied def after the blank line that follows it.
    src = b"object A {\n  def f(x: Int): Int =\n    x + 1\n\n}\n"
    root = parse_source("scala", src).root_node
    body = root.named_children[0].child_by_field_name("body")
    fn = next(c for c in body.named_children if c.type == "function_definition")
    assert fn.end_point == (4, 0)  # column 0 of the line after the blank line
    assert line_span(fn) == (2, 3)
