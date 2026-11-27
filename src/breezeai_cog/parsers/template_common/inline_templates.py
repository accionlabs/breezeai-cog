"""Markup written as a string inside a code file: a component's ``template: '…'``.

An Angular ``@Component({ template: `…` })``, an AngularJS ``.component('x', { template: '…' })``
and a Vue ``defineComponent({ template: `…` })`` keep their markup in the ``.ts`` / ``.js`` file,
not in a template file. :func:`inline_template_markup` returns a copy of the file, the same
length, that keeps only those strings' contents and blanks everything else, so the framework's
template reader parses it with true line numbers.

Only a plain string is read: a template literal with a ``${…}`` part is built at run time, and
one that holds no tag is not markup — both are left alone (honest-null). The ``template`` key is
the one these frameworks read; ``templateUrl`` (a file) is handled by the html index.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from .script_blocks import blank

#: A tag opening somewhere in the string: the cheapest sign that it is markup.
_TAG = re.compile(rb"<[A-Za-z]")
#: A ``template:`` key (``templateUrl:`` does not match): files without one are not walked.
_TEMPLATE_KEY = re.compile(rb"""\btemplate['"]?\s*:""")


def inline_template_markup(root: Node, source: bytes) -> bytes | None:
    """A same-length copy of ``source`` keeping only the contents of its ``template: '…'``
    strings, or None when it has none."""
    if _TEMPLATE_KEY.search(source) is None:
        return None  # cheap guard — most code files, and every templateUrl component, have none
    kept: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "pair":
            span = _template_string(node, source)
            if span is not None:
                kept.append(span)
            continue
        stack.extend(node.named_children)
    if not kept:
        return None
    markup = bytearray(source)
    blank(markup, 0, len(markup))
    for start, end in kept:
        markup[start:end] = source[start:end]
    return bytes(markup)


def _template_string(pair: Node, source: bytes) -> tuple[int, int] | None:
    key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
    if key is None or value is None:
        return None
    if source[key.start_byte : key.end_byte].strip(b"'\"") != b"template":
        return None
    if value.type not in ("string", "template_string"):
        return None
    if any(c.type == "template_substitution" for c in value.named_children):
        return None  # built at run time
    start, end = value.start_byte + 1, value.end_byte - 1  # inside the quotes
    return (start, end) if _TAG.search(source, start, end) else None
