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
