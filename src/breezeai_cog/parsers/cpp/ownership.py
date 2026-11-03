"""C++'s ownership proof: C and C++ both register ``.h``.

The ownership step (``core/ownership.py``) reads every C and C++ file as bytes, before any
parse. Both languages report the headers a file includes
(``cfamily_common/imports.py::include_targets``); a header included only from C files is C,
only from C++ files C++, and from both C (C++ can include a C header; C cannot include a C++
one). C++ also supplies:

* :func:`cpp_only_syntax` — whether a header uses syntax C does not have. Comments and string
  literals are ignored. Inside ``#if`` groups that test ``__cplusplus`` a C API header routinely
  adapts itself for C++ includers — its ``extern "C" {`` guard, C++ includes (``<cstddef>``),
  ``using namespace std;`` — and none of that is proof. A C++ *declaration* there
  (``namespace``, ``class``, ``template``, ``std::``) is: the header is written for both
  languages, and only the C++ grammar reads all of it — the C grammar would lose the C++
  declarations.

The markers are deliberately few and unambiguous: one that a valid C file could contain would
send a C header to the C++ grammar on its own.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(rb"/\*.*?\*/|//[^\n]*", re.S)
_STRING = re.compile(rb'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_DIRECTIVE = re.compile(rb"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")

#: ``extern "C"`` / ``extern "C++"`` — a linkage specification, read before strings are removed.
_LINKAGE = re.compile(rb'\bextern[ \t]*"C(?:\+\+)?"')
#: C++ declarations a C compiler rejects — proof wherever they are. ``class`` is a valid C
#: identifier, so only ``class Name`` followed by a base list, a body or ``;`` counts, and never
#: Objective-C's ``@class``.
_CPP_DECLARATION = re.compile(
    rb"(?<![@\w])class[ \t\r\n]+[A-Za-z_]\w*(?:[ \t\r\n]+final)?[ \t\r\n]*[:{;]"
    rb"|\bnamespace(?:[ \t\r\n]+[A-Za-z_][\w:]*)?[ \t\r\n]*\{"
    rb"|\btemplate[ \t\r\n]*<"
    rb"|\bstd::"
)
#: Proof only outside ``__cplusplus`` groups, where a C header adapting itself for C++ includers
#: may have them: a standard-library header with no extension (``<vector>``, ``<cstddef>`` —
#: every C one ends in ``.h``), and ``using namespace``.
_CPP_ADAPTER = re.compile(
    rb"^[ \t]*#[ \t]*include[ \t]*<[a-z_]+>|\busing[ \t]+namespace\b", re.M
)


def _split_cplusplus_blocks(source: bytes) -> tuple[bytes, bytes]:
    """``source`` split into the lines outside every ``#if`` group that tests ``__cplusplus``,
    and the lines inside one (all its branches: the ``#else`` of ``#ifdef __cplusplus`` is C,
    which needs no proof)."""
    outside: list[bytes] = []
    inside: list[bytes] = []
    depth = 0  # nesting inside a __cplusplus group (0: outside)
    for line in source.splitlines():
        m = _DIRECTIVE.match(line)
        kind = m.group(1) if m else None
        if depth:
            if kind in (b"if", b"ifdef", b"ifndef"):
                depth += 1
            elif kind == b"endif":
                depth -= 1
            inside.append(line)
            continue
        if m is not None and kind in (b"if", b"ifdef", b"ifndef", b"elif") and (
            b"__cplusplus" in m.group(2)
        ):
            depth = 1
            continue
        outside.append(line)
    return b"\n".join(outside), b"\n".join(inside)


def cpp_only_syntax(source: bytes) -> bool:
    """True when ``source`` uses syntax only C++ has: a C++ declaration anywhere, or a linkage
    specification, a C++ library include or ``using namespace`` outside ``__cplusplus`` groups. Comments and string
    literals never count."""
    outside, inside = _split_cplusplus_blocks(_COMMENT.sub(b" ", source))
    if _LINKAGE.search(outside) or _CPP_ADAPTER.search(_STRING.sub(b'""', outside)):
        return True
    return any(_CPP_DECLARATION.search(_STRING.sub(b'""', code)) for code in (outside, inside))
