"""Java string literals fold with their escapes decoded; what cannot be read stays None."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.constfold import init_tokens, java_literal_value
from breezeai_cog.parsers.treesitter import parse_source


def _literal(language: str, code: bytes):
    tree = parse_source(language, code)
    found = []

    def walk(n) -> None:
        if n.type == "string_literal":
            found.append(n)
        for c in n.named_children:
            walk(c)

    walk(tree.root_node)
    return found[0]


@pytest.mark.parametrize(
    "literal, value",
    [
        (rb'"plain"', "plain"),
        (rb'""', ""),
        (rb'"a\"b"', 'a"b'),
        (rb'"t\tn\nr\rs\sb\bf\f"', "t\tn\nr\rs b\bf\f"),
        (rb'"q\'\\e"', "q'\\e"),
        (rb'"\101\0\377"', "A\x00\xff"),
        (rb'"\u00e9\u0041"', "\u00e9A"),
        (rb'"\ud83d\ude00"', "\U0001f600"),
    ],
)
def test_java_literal_value(literal: bytes, value: str) -> None:
    code = b"class A { String s = " + literal + b"; }"
    assert java_literal_value(_literal("java", code), code) == value


def test_a_text_block_or_lone_surrogate_is_not_read() -> None:
    block = b'class A { String s = """\n  x\n  """; }'
    assert java_literal_value(_literal("java", block), block) is None
    assert init_tokens(_literal("java", block), block) is None
    lone = b'class A { String s = "\\ud83d"; }'
    assert java_literal_value(_literal("java", lone), lone) is None


def test_init_tokens_decodes_java_escapes() -> None:
    code = b'class A { String s = "p3.with\\"quote"; }'
    assert init_tokens(_literal("java", code), code) == [("lit", 'p3.with"quote')]


def test_groovy_literals_are_unchanged() -> None:
    code = b'class A { static final String s = "x\\"q" }'
    assert init_tokens(_literal("groovy", code), code) == [("lit", 'x\\"q')]
