"""C and C++ class / struct / union / enum extraction → Class + flat members + statements.

A ``class_specifier`` / ``struct_specifier`` has a ``name`` (type_identifier), an
optional unnamed ``base_class_clause`` child (its ``type_identifier`` bases → the
heritage), and a ``body`` (field_declaration_list). Inside the body, a
``field_declaration`` whose declarator is a ``function_declarator`` is a member-function
declaration (emitted as a flat method ``Function``); one whose declarator is a plain
``field_identifier`` is a member variable (not a function — skipped).

A struct is a class with ``type="struct"``. Members default to ``public`` in a struct
and ``private`` in a class, updated by ``access_specifier`` labels in the body."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ClassType, ConstructorParam, Decorator, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import emit_enum_members, member_statement
from ..treesitter import line_span, node_text, parse_source
from .functions import (
    ATTRIBUTE_TYPES,
    attribute_decorators,
    build_member_function,
    decorators_of,
    extract_params,
    is_macro_call_declarator,
    function_declarator_of,
    is_function_pointer,
    read_member,
)
from .mappings import KEYWORDS, MACRO_TYPES
from .statements import macro_statement

#: Aggregate specifiers that become a ``Class`` node, mapped to their ``type``. A union is
#: a distinct kind (overlapping storage, one member active at a time) — it is not a struct,
#: so it carries its own type rather than being flattened to ``struct``.
_AGGREGATE_TYPE: dict[str, ClassType] = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "union",
}
_CLASS_TYPES = tuple(_AGGREGATE_TYPE)
#: Type-definition specifiers that can be *nested* inside a class body.
_NESTED_TYPE_SPECS = (*_CLASS_TYPES, "enum_specifier")


def _nested_type_spec(member: Node) -> Node | None:
    """The specifier of a nested type defined in a class body, or ``None`` for a real member.
    A nested ``union Chunk { … };`` / ``enum Kind { … };`` parses as a ``field_declaration``
    whose ``type`` is the specifier (not as a bare specifier), so both forms are unwrapped
    here. A member variable (``Chunk* root;``, or C's ``struct node *next;``, whose type is a
    specifier without a body) returns ``None``."""
    if member.type in _NESTED_TYPE_SPECS:
        return member
    if member.type == "field_declaration":
        tnode = member.child_by_field_name("type")
        if tnode is not None and tnode.type in _NESTED_TYPE_SPECS and _defines(tnode):
            return tnode
    return None


def _defines(spec: Node) -> bool:
    """Whether a struct/class/union/enum specifier has a body (defines the type) rather than
    naming it (``struct node``)."""
    return spec.child_by_field_name("body") is not None or any(
        c.type == "enumerator_list" for c in spec.named_children
    )
#: Class-body members that may be a function: a method with a return type is a
#: ``field_declaration``; a constructor/destructor (no return type) is a ``declaration``;
#: an inline definition (or ``= default``) is a ``function_definition``.
_MEMBER_FN_TYPES = ("field_declaration", "function_definition", "declaration")

#: ``class WORD Name [final] :`` or ``{`` — a word between the class keyword and the class
#: name. Only a candidate: the tree decides (:func:`_class_macro_words`).
_CLASS_HEAD_WITH_WORD = re.compile(
    rb"\b(?:class|struct|union)\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:final\s*)?(?::(?!:)|\{)"
)
_SPECIFIERS = ("class_specifier", "struct_specifier", "union_specifier")

#: A macro's byte span and the class name's byte span after it.
_MacroWord = tuple[tuple[int, int], tuple[int, int]]


def _class_macro_words(root: Node, source: bytes) -> list[_MacroWord]:
    """Macros written between ``class`` and the class name: ``class OPENVINO_API GatherMatmul
    : public Op { … }``. In C++ only an attribute can sit there, and ``alignas`` / ``[[…]]``
    parse; a bare word is an export macro the grammar can't expand. It takes the macro for the
    class name and reads the class as a function or a broken declaration, so the class is lost.
    A word counts only when the grammar did misread it that way: the name of a head with no
    body, whose declaration is a function definition or has an error — a clean ``struct Point
    p{1, 2};`` is a variable."""
    words = []
    for m in _CLASS_HEAD_WITH_WORD.finditer(source):
        word = root.descendant_for_byte_range(m.start(1), m.end(1))
        head = word.parent if word is not None else None
        if (
            word is None or head is None or word.type != "type_identifier"
            or word.start_byte != m.start(1) or word.end_byte != m.end(1)
            or head.type not in _SPECIFIERS or head.child_by_field_name("body") is not None
        ):
            continue
        owner = head.parent
        if owner is not None and (owner.type == "function_definition" or owner.has_error):
            words.append(((m.start(1), m.end(1)), (m.start(2), m.end(2))))
    return words


def _reads_as_class(root: Node, name_span: tuple[int, int]) -> bool:
    """Whether the name at ``name_span`` heads a class with a body, outside any error — the
    class parsed whole once its macro was blanked."""
    name = root.descendant_for_byte_range(*name_span)
    head = name.parent if name is not None else None
    if (
        name is None or head is None or name.type != "type_identifier"
        or (name.start_byte, name.end_byte) != name_span
        or head.type not in _SPECIFIERS or head.child_by_field_name("body") is None
    ):
        return False
    node: Node | None = head
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


_WORD = re.compile(rb"[A-Za-z_]\w*")


def _head_decorators(node: Node, source: bytes) -> list[Decorator]:
    """The attributes and the export macro between ``class`` and the class name, in source
    order: ``class [[deprecated]] __declspec(dllexport) K``, ``class OPENVINO_API Op``. A macro
    there was blanked before parsing (:func:`parse_cfamily`), so it is the one word in the
    original source that no node covers."""
    name = node.child_by_field_name("name")
    if name is None or not node.children:
        return []
    out: list[Decorator] = []
    cursor = node.children[0].end_byte  # after `class` / `struct` / `union`
    for c in node.children[1:]:
        if c.start_byte >= name.start_byte:
            break
        out.extend(Decorator(name=w.decode()) for w in _WORD.findall(source[cursor:c.start_byte]))
        if c.type in ATTRIBUTE_TYPES:
            out.extend(attribute_decorators(c, source))
        cursor = c.end_byte
    out.extend(Decorator(name=w.decode()) for w in _WORD.findall(source[cursor:name.start_byte]))
    return out


#: ``NAME(`` at the start of a line: a candidate wrapper macro around a declaration.
_WRAPPER_HEAD = re.compile(rb"^[ \t]*([A-Za-z_]\w*)[ \t]*\(", re.M)
#: The arguments after a wrapper's declaration: string literals, commas and comments only.
_STRING_ARGS = re.compile(rb'(?:\s|,|//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*")*', re.S)
_PARAMS_END = re.compile(rb"\)(?:\s|//[^\n]*|/\*.*?\*/)*\Z", re.S)


def _close_paren(source: bytes, start: int) -> tuple[int, int | None] | None:
    """For an argument list opened just before ``start``: the offset of its closing ``)`` and
    of its first top-level comma (``None`` when it has one argument). Strings, character
    literals and comments are skipped; ``None`` when it never closes."""
    depth, comma, i, n = 1, None, start, len(source)
    while i < n:
        c = source[i]
        if c in (0x22, 0x27):  # a string or character literal
            quote, i = c, i + 1
            while i < n and source[i] != quote:
                i += 2 if source[i] == 0x5C else 1
        elif source.startswith(b"//", i):
            i = source.find(b"\n", i)
            if i < 0:
                return None
        elif source.startswith(b"/*", i):
            i = source.find(b"*/", i)
            if i < 0:
                return None
            i += 1
        elif c in b"([{":
            depth += 1
        elif c in b")]}":
            depth -= 1
            if depth == 0:
                return i, comma
        elif c == 0x2C and depth == 1 and comma is None:
            comma = i
        i += 1
    return None


#: A wrapper's two blanked spans (``NAME(`` and ``, "msg")``) and where its declaration starts.
_Wrapper = tuple[tuple[int, int], tuple[int, int], int]


def _declaration_wrappers(root: Node, source: bytes) -> list[_Wrapper]:
    """Macros wrapped around a whole declaration: ``DEPRECATED(LLAMA_API int f(int a), "use
    g");``. The grammar can't read a declaration as a macro argument, and its error runs on
    over the clean declarations after it. A wrapper counts only outside a function body, with
    a first argument ending in a parameter list, string literals as its other arguments, and
    ``;`` right after it."""
    if not root.has_error:
        return []
    found = []
    for m in _WRAPPER_HEAD.finditer(source):
        node: Node | None = root.descendant_for_byte_range(m.start(1), m.end(1))
        while node is not None and node.type != "compound_statement":
            node = node.parent
        if node is not None:
            continue  # inside a function body: a call
        closed = _close_paren(source, m.end())
        if closed is None:
            continue
        close, comma = closed
        if comma is None:
            continue
        first = source[m.end():comma]
        if (
            b"(" not in first or _PARAMS_END.search(first) is None
            or _STRING_ARGS.fullmatch(source, comma, close) is None
            or b'"' not in source[comma:close]
            or not source[close + 1:close + 64].lstrip().startswith(b";")
        ):
            continue
        decl = m.end() + len(first) - len(first.lstrip())
        found.append(((m.start(1), m.end()), (comma, close + 1), decl))
    return found


def _reads_as_declaration(root: Node, wrapper: _Wrapper) -> bool:
    """Whether a wrapper's declaration parsed whole once the wrapper was blanked: one
    declaration from its first word through the ``;`` after the wrapper, outside any error.
    Whether its name can be trusted is ``functions.read_declaration``'s to say, as for any other."""
    (_, _), (_, end), start = wrapper
    node: Node | None = root.descendant_for_byte_range(start, start)
    while node is not None and not (node.type == "declaration" and node.start_byte == start):
        node = node.parent
    if node is None or node.end_byte <= end:
        return False
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


#: What may end the text before a member: the end of a member, the body's ``{`` or an access
#: label.
_MEMBER_BEFORE = re.compile(rb"(?:[;{}]|\b(?:public|protected|private)[ \t]*:)\Z")
#: What continues a declaration rather than starting a new member: a constructor's initializer
#: list or body, ``= default``, a qualifier, a trailing return type, an attribute.
_DECLARATION_GOES_ON = re.compile(
    rb"(?:[{:=;,)&]|->|\[\[|(?:const|volatile|noexcept|override|final|try|throw|requires"
    rb"|__attribute__)\b)"
)
_MEMBER_START = re.compile(rb"[A-Za-z_~}#]")
_CLASS_KEYWORDS = ("class", "struct", "union")
_LINE_COMMENT = re.compile(rb"//[^\n]*")


def _text_before(source: bytes, at: int) -> bytes:
    """The code before ``at``, without trailing blank and ``//``-comment lines."""
    end = at
    while end > 0:
        start = source.rfind(b"\n", 0, end - 1) + 1
        line = _LINE_COMMENT.sub(b"", source[start:end]).strip()
        if line:
            return line
        end = start
    return b""


def _member_macros(root: Node, source: bytes) -> list[tuple[int, int]]:
    """Macros written as a whole member of a class body: ``OPENVINO_MATCHER_PASS_RTTI("…")``.
    A line that is only ``NAME(…)`` has no ``;`` and no body, so it can't be a member; the
    grammar reads it as the start of a field and runs on into the next member, breaking the
    class. A line counts only where the grammar failed on it in a class body (a field with an
    error, or an error holding the class keyword before it), outside a function body, after the end of a member or an access label, and followed by the
    start of another member — not ``:``, ``{``, ``=`` or a qualifier, which would make it a
    constructor written over two lines."""
    if not root.has_error:
        return []
    found = []
    for m in _WRAPPER_HEAD.finditer(source):
        node: Node | None = root.descendant_for_byte_range(m.start(1), m.end(1))
        in_class = False
        while node is not None and node.type != "compound_statement":
            in_class = in_class or (
                node.type == "field_declaration" and node.has_error
            ) or (
                node.type == "ERROR" and any(
                    c.type in _CLASS_KEYWORDS and c.start_byte < m.start() for c in node.children
                )
            )
            node = node.parent
        if node is not None or not in_class:
            continue  # clean, outside a class body, or inside a function body
        closed = _close_paren(source, m.end())
        if closed is None:
            continue
        end = closed[0] + 1
        line_end = source.find(b"\n", end)
        line_end = len(source) if line_end < 0 else line_end
        if _LINE_COMMENT.sub(b"", source[end:line_end]).strip():
            continue  # more on the line
        after = _LINE_COMMENT.sub(b"", source[line_end:line_end + 256]).lstrip()
        if (
            _MEMBER_BEFORE.search(_text_before(source, m.start(1))) is None
            or not _MEMBER_START.match(after) or _DECLARATION_GOES_ON.match(after)
        ):
            continue
        found.append((m.start(1), end))
    return found


def _reads_as_members(root: Node, span: tuple[int, int]) -> bool:
    """Whether a blanked member macro now sits in a class body, outside any error, with the
    member after it parsed whole."""
    start, end = span
    body = root.descendant_for_byte_range(start, end)
    while body is not None and body.type != "field_declaration_list":
        if body.type == "ERROR":
            return False
        body = body.parent
    if body is None or body.parent is None or body.parent.type not in _SPECIFIERS:
        return False
    following = next((c for c in body.children if c.start_byte >= end), None)
    if following is None or following.type == "ERROR" or following.has_error:
        return False
    node: Node | None = body
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


#: ``) NAME(args);`` or ``) NAME;`` — a candidate attribute macro after a declaration's
#: parameter list: ``void errorf(const char *fmt, ...) CURL_PRINTF(1, 2);``.
_TRAILING_MACRO = re.compile(rb"\)[ \t\r\n]*(?:([A-Za-z_]\w*)[ \t]*\(|([A-Z][A-Z0-9_]*)[ \t\r\n]*;)")
_SEMICOLON_NEXT = re.compile(rb"[ \t\r\n]*;")
_DECLARATION_TYPES = ("declaration", "field_declaration")

#: A trailing macro's blanked span, and the offsets of the ``)`` before it and the ``;`` after.
_Trailing = tuple[tuple[int, int], int, int]


def _trailing_macros(root: Node, source: bytes) -> list[_Trailing]:
    """Attribute macros after a declaration's parameter list: ``void errorf(const char *fmt,
    ...) CURL_PRINTF(1, 2);``, ``CURLcode f(void) WARN_UNUSED_RESULT;``. They expand to
    ``__attribute__((…))``, which the grammar reads there, but a macro breaks the declaration. A
    candidate counts only where the grammar failed on it or cut the declaration before it short,
    outside a function body; a bare word
    only in capitals (``int f(a) int a;`` is a K&R definition)."""
    if not root.has_error:
        return []
    found = []
    for m in _TRAILING_MACRO.finditer(source):
        paren = root.descendant_for_byte_range(m.start(), m.start() + 1)
        if paren is None or paren.type != ")" or paren.parent is None or (
            paren.parent.type != "parameter_list"
        ):
            continue  # not the end of a parameter list (a comment, a macro body, a call)
        word = 1 if m.group(1) is not None else 2
        failed, in_body = False, False
        for at in (m.start(), m.start(word)):  # the `)` (a declaration cut short) and the macro
            node: Node | None = root.descendant_for_byte_range(at, at + 1)
            while node is not None and node.type != "compound_statement":
                if node.type == "ERROR" or (node.type in _DECLARATION_TYPES and node.has_error):
                    failed = True
                node = node.parent
            in_body = in_body or node is not None
        if in_body or not failed:
            continue
        end = m.end(2)
        if word == 1:
            closed = _close_paren(source, m.end())
            if closed is None:
                continue
            end = closed[0] + 1
        semicolon = _SEMICOLON_NEXT.match(source, end)
        if semicolon is None:
            continue
        found.append(((m.start(word), end), m.start(), semicolon.end() - 1))
    return found


def _reads_with_trailing_macro_gone(root: Node, trailing: _Trailing) -> bool:
    """Whether a blanked trailing macro left a declaration that parses whole: its parameter list
    ends at the ``)`` and the declaration at the ``;``, outside any error."""
    _, paren, semicolon = trailing
    node: Node | None = root.descendant_for_byte_range(paren, paren + 1)
    if node is None or node.parent is None or node.parent.type != "parameter_list":
        return False
    while node is not None and node.type not in _DECLARATION_TYPES:
        node = node.parent
    if node is None or node.end_byte != semicolon + 1:
        return False
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


_TAG_KEYWORDS = (b"struct", b"union", b"enum")


def _tag_type_macros(root: Node, source: bytes) -> list[tuple[tuple[int, int], int]]:
    """Macros in front of a ``struct`` / ``union`` / ``enum`` type: ``UNITTEST struct stsentry
    *hsts_check(…)``, ``CURL_EXTERN struct curl_slist *curl_slist_append(…);``. The C grammar
    splits such a line into a broken ``UNITTEST struct`` (its ``;`` missing) and ``stsentry
    *hsts_check(…)``, losing the tag keyword and the macro. A macro counts only in exactly that
    split: a declaration of a type name, the keyword and a missing ``;``, followed at once by a
    declaration or definition whose type is a plain name."""
    if not root.has_error:
        return []
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        for c in node.children:
            if c.type in ("compound_statement", "field_declaration_list"):
                continue
            stack.append(c)
        if node.type != "declaration":
            continue
        kids = [c for c in node.children if c.type != "storage_class_specifier"]
        after = node.next_sibling
        if (
            len(kids) != 3 or kids[0].type != "type_identifier"
            or kids[1].type != "identifier"
            or source[kids[1].start_byte:kids[1].end_byte] not in _TAG_KEYWORDS
            or not kids[2].is_missing
            or after is None or after.type not in ("declaration", "function_definition")
            or (ret := after.child_by_field_name("type")) is None
            or ret.type != "type_identifier" or after.start_byte != ret.start_byte
        ):
            continue
        found.append(((kids[0].start_byte, kids[0].end_byte), kids[1].start_byte))
    return found


def _reads_with_tag_type(root: Node, tagged: tuple[tuple[int, int], int]) -> bool:
    """Whether a blanked macro left a declaration or definition whose type is the tagged type
    (starting at its keyword), outside any error and with no error of its own in its header."""
    keyword = tagged[1]
    node: Node | None = root.descendant_for_byte_range(keyword, keyword + 1)
    while node is not None and node.type not in ("declaration", "function_definition"):
        node = node.parent
    if node is None:
        return False
    ret = node.child_by_field_name("type")
    if (
        ret is None or ret.type not in ("struct_specifier", "union_specifier", "enum_specifier")
        or ret.start_byte != keyword
        or any(c.type == "ERROR" or c.is_missing for c in node.children)
    ):
        return False
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


def _misread_qualifier_macros(root: Node, source: bytes) -> list[tuple[tuple[int, int], int]]:
    """Qualifier macros in a C++ definition read as a variable: ``void unpack(tile_t * RESTRICT
    tile, const void * RESTRICT p) { … }``. When no parameter has an unambiguous type, C++ reads
    the name and parentheses as a variable initialised with ``tile_t * RESTRICT`` (``tile`` left
    in an error) and the body as a stray block. A macro counts only in exactly that shape: a
    declaration whose one declarator is initialised with an argument list, followed at once by
    ``{ … }``, where an ``A * MACRO`` argument is followed by an error starting with one word (the
    name; the parameters after it may be in the error too). Its
    start is kept so the second parse can be checked to read a definition there."""
    if not root.has_error:
        return []
    found: list[tuple[tuple[int, int], int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        for c in node.children:
            if c.type not in ("compound_statement", "field_declaration_list"):
                stack.append(c)
        if node.type != "declaration":
            continue
        holder = node.parent if node.parent is not None and node.parent.type == "template_declaration" else node
        body = holder.next_sibling
        init = node.child_by_field_name("declarator")
        args = init.child_by_field_name("value") if init is not None and init.type == "init_declarator" else None
        if body is None or body.type != "compound_statement" or args is None or args.type != "argument_list":
            continue
        macros = []
        for c in args.named_children:
            err = c.next_sibling
            right = c.child_by_field_name("right") if c.type == "binary_expression" else None
            op = c.child_by_field_name("operator") if right is not None else None
            if (
                right is not None and op is not None and right.type == "identifier"
                and source[op.start_byte:right.start_byte].strip() == b"*"
                and err is not None and err.type == "ERROR"
                and err.children and err.children[0].type == "identifier"
                and (err.child_count == 1 or err.children[1].type == ",")
            ):
                macros.append((right.start_byte, right.end_byte))
        if macros:
            found.extend((m, node.start_byte) for m in macros)
    return found


def _reads_as_definition(root: Node, macro: tuple[tuple[int, int], int]) -> bool:
    """Whether a function definition starts where the misread declaration did, outside any error
    and with a clean header."""
    start = macro[1]
    node: Node | None = root.descendant_for_byte_range(start, start + 1)
    while node is not None and not (node.type == "function_definition" and node.start_byte == start):
        node = node.parent
    if node is None or any(c.type == "ERROR" or c.is_missing for c in node.children):
        return False
    declarator = node.child_by_field_name("declarator")
    if declarator is None or declarator.has_error:
        return False
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


def _blank(source: bytes, spans: list[tuple[int, int]]) -> bytes:
    """``source`` with ``spans`` turned to spaces; newlines stay, so lines and offsets don't
    move."""
    out = bytearray(source)
    for start, end in spans:
        out[start:end] = bytes(b if b == 0x0A else 0x20 for b in source[start:end])
    return bytes(out)


#: ``#ifdef __cplusplus`` / ``#if defined(__cplusplus)`` holding one line, then ``#endif``: the
#: two halves of the guard around a C API, ``extern "C" {`` and ``}``.
_GUARD = (
    rb'^[ \t]*#[ \t]*if(?:def[ \t]+__cplusplus|[ \t]+defined[ \t]*\(?[ \t]*__cplusplus[ \t]*\)?)'
    rb'[ \t]*(?://[^\n]*)?\n([ \t]*%s[^\n]*\n)[ \t]*#[ \t]*endif[^\n]*'
)
_GUARD_OPEN = re.compile(_GUARD % rb'extern[ \t]+"C"[ \t]*\{', re.M)
_GUARD_CLOSE = re.compile(_GUARD % rb'\}', re.M)

#: A guard pair's blanked spans and the region it encloses.
_Guard = tuple[list[tuple[int, int]], tuple[int, int]]


def _collapsed_guards(root: Node, source: bytes, grammar: str) -> list[_Guard]:
    """The ``extern "C"`` guard of a file whose parse collapsed around it. Both ``#ifdef
    __cplusplus`` blocks are read at once, so ``{`` and ``}`` sit in separate conditionals; when
    the grammar's recovery fails, the file reads as one error. ``__cplusplus`` is defined exactly
    when a C++ compiler reads the file, so the guard is resolved as that compiler would: for C++
    and CUDA only the directive lines go, and ``extern "C" { … }`` stays a block; for C the whole
    guard goes. Only a file with an error at file scope, and with one guard pair, counts."""
    if root.type != "ERROR" and not any(c.type == "ERROR" for c in root.children):
        return []
    opens, closes = list(_GUARD_OPEN.finditer(source)), list(_GUARD_CLOSE.finditer(source))
    if len(opens) != 1 or len(closes) != 1 or opens[0].end() > closes[0].start():
        return []
    spans = []
    for m in (opens[0], closes[0]):
        if grammar == "c":
            spans.append((m.start(), m.end()))
        else:
            spans += [(m.start(), m.start(1)), (m.end(1), m.end())]
    return [(spans, (opens[0].start(), closes[0].end()))]


def _reads_without_collapse(root: Node, region: tuple[int, int]) -> bool:
    """Whether no file-scope error overlaps a resolved guard's region."""
    if root.type == "ERROR":
        return False
    start, end = region
    return not any(
        c.type == "ERROR" and c.start_byte < end and c.end_byte > start for c in root.children
    )


def parse_cfamily(grammar: str, source: bytes, timeout_micros: int) -> Node:
    """Parse a C-family file. Two macros the grammar can't expand hide what they are written on,
    and are blanked to spaces for a second parse:

    * a macro between ``class`` and the class name (:func:`_class_macro_words`): the class then
      reads as written without it;
    * a macro wrapped around a declaration (:func:`_declaration_wrappers`): ``DEPRECATED(`` and
      ``, "msg")`` go, and the declaration reads as written;
    * a macro written as a whole class member (:func:`_member_macros`): the line goes, and the
      members around it read as written;
    * an attribute macro after a declaration's parameter list (:func:`_trailing_macros`):
      ``CURL_PRINTF(1, 2)`` goes, and the declaration reads as written;
    * a macro in front of a ``struct`` / ``union`` / ``enum`` type (:func:`_tag_type_macros`):
      ``UNITTEST`` goes, and ``struct stsentry *hsts_check(…)`` reads as written;
    * a qualifier macro that made a C++ definition read as a variable
      (:func:`_misread_qualifier_macros`): ``RESTRICT`` goes, and the definition reads as written;

    and an ``extern "C"`` guard the file's parse collapsed around is resolved as the compiler
    for the grammar's language would (:func:`_collapsed_guards`).

    Lines and byte offsets don't move, so names and text are still read from the original
    ``source``. A blank is kept only where what it hid now parses whole (a class with its body,
    a declaration without an error); if one doesn't, the others are tried once more alone."""
    root = parse_source(grammar, source, timeout_micros).root_node
    words = _class_macro_words(root, source)
    wrappers = _declaration_wrappers(root, source)
    guards = _collapsed_guards(root, source, grammar)
    members = _member_macros(root, source)
    trailing = _trailing_macros(root, source)
    tagged = _tag_type_macros(root, source)
    qualifiers = _misread_qualifier_macros(root, source) if grammar != "c" else []
    for _ in range(2):  # blank all; if some still break, blank only the others once
        if not (words or wrappers or guards or members or trailing or tagged or qualifiers):
            return root
        spans = [w[0] for w in words] + [s for w in wrappers for s in w[:2]]
        spans += [s for g in guards for s in g[0]] + members + [t[0] for t in trailing]
        spans += [t[0] for t in tagged] + [q[0] for q in qualifiers]
        reparsed = parse_source(grammar, _blank(source, spans), timeout_micros).root_node
        whole_words = [w for w in words if _reads_as_class(reparsed, w[1])]
        whole_wrappers = [w for w in wrappers if _reads_as_declaration(reparsed, w)]
        whole_guards = [g for g in guards if _reads_without_collapse(reparsed, g[1])]
        whole_members = [s for s in members if _reads_as_members(reparsed, s)]
        whole_trailing = [t for t in trailing if _reads_with_trailing_macro_gone(reparsed, t)]
        whole_tagged = [t for t in tagged if _reads_with_tag_type(reparsed, t)]
        whole_qualifiers = [q for q in qualifiers if _reads_as_definition(reparsed, q)]
        if (whole_words, whole_wrappers, whole_guards, whole_members, whole_trailing,
                whole_tagged, whole_qualifiers) == (
                    words, wrappers, guards, members, trailing, tagged, qualifiers):
            return reparsed
        words, wrappers, guards = whole_words, whole_wrappers, whole_guards
        members, trailing, tagged = whole_members, whole_trailing, whole_tagged
        qualifiers = whole_qualifiers
    return root


def typedef_type(node: Node, source: bytes) -> tuple[Node, str, str | None] | None:
    """``typedef struct [tag] { … } name;`` (also ``union`` / ``enum``) → the specifier, the
    typedef name and the tag (``None`` when anonymous). ``None`` when the typedef doesn't
    define the type (``typedef struct node node_t;``, no body) or doesn't give it exactly one
    plain name (``typedef struct {…} *list_ptr;``)."""
    if node.type != "type_definition":
        return None
    spec = node.child_by_field_name("type")
    if spec is None or spec.type not in _NESTED_TYPE_SPECS:
        return None
    if not _defines(spec):
        return None
    names = [d for d in node.children_by_field_name("declarator") if d.type == "type_identifier"]
    if len(names) != 1:
        return None
    tag = spec.child_by_field_name("name")
    return spec, node_text(names[0], source), node_text(tag, source) if tag is not None else None


def defined_type(node: Node) -> Node | None:
    """The named struct / class / union / enum a ``declaration`` defines along with its
    variables (``struct point { int x, y; } origin;``), or ``None``. An anonymous one has no
    name to give a class."""
    if node.type != "declaration":
        return None
    spec = node.child_by_field_name("type")
    if (
        spec is None or spec.type not in _NESTED_TYPE_SPECS or not _defines(spec)
        or spec.child_by_field_name("name") is None
    ):
        return None
    return spec


#: The two C++ type-alias declarations. ``using namespace …`` / ``using Ns::name`` are a
#: ``using_declaration`` instead — an import, deliberately not in this set.
TYPE_ALIAS_TYPES = ("type_definition", "alias_declaration")


def _base_name(base: Node, source: bytes) -> str | None:
    """The base-class name from a ``base_class_clause`` entry — a ``type_identifier``,
    the head of a ``template_type`` (``Base<T>`` → ``Base``), or a ``qualified_identifier``."""
    if base.type == "type_identifier":
        return node_text(base, source)
    if base.type == "template_type":
        ti = base.child_by_field_name("name") or next(
            (c for c in base.named_children if c.type == "type_identifier"), None
        )
        return node_text(ti, source) if ti is not None else None
    if base.type == "qualified_identifier":
        return node_text(base, source)
    return None


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """(extends, implements) from the ``base_class_clause``. C++ has multiple
    inheritance — the first base is ``extends``, the rest are ``implements``."""
    clause = next((c for c in node.named_children if c.type == "base_class_clause"), None)
    if clause is None:
        return None, []
    bases: list[str] = []
    for c in clause.named_children:
        name = _base_name(c, source)
        if name is not None:
            bases.append(name)
    if not bases:
        return None, []
    return bases[0], bases[1:]


#: Nodes that *are* a declared name. ``type_identifier`` covers a ``typedef``'s new type
#: name (``typedef int Handle`` → ``Handle``), which is never how a variable/field name is
#: spelled — a field's name is an ``identifier``/``field_identifier``, and its type rides
#: the separate ``type`` field — so accepting it here is unambiguous.
_NAME_NODES = ("field_identifier", "identifier", "type_identifier")


def _declarator_name(node: Node | None, source: bytes) -> str | None:
    """Innermost declared name, unwrapping pointer/reference/array/init/function
    declarators (``* kName`` → ``kName``, ``(*Callback)(int)`` → ``Callback``). A keyword is
    never one: the grammar misread a macro."""
    if node is None:
        return None
    if node.type in _NAME_NODES:
        text = node_text(node, source)
        return text if text not in KEYWORDS else None
    inner = node.child_by_field_name("declarator") or next(
        (c for c in node.named_children
         if c.type in _NAME_NODES or c.type.endswith("declarator")),
        None,
    )
    return _declarator_name(inner, source) if inner is not None else None


def type_alias_statement(
    node: Node, source: bytes, path: str, *, parent_id: str, limit: int, seen_ids: set[str]
) -> Statement:
    """One flat Statement for a C++ type alias — ``typedef X Y;`` (``type_definition``, the
    new name on its declarator) or ``using Y = X;`` (``alias_declaration``, a ``name``
    field). The aliased type stays on ``text``, which is what makes it queryable.

    Only these two node types are aliases: ``using namespace std;`` and ``using std::swap;``
    are a ``using_declaration`` (an import, not a declaration) and never reach here."""
    declarator = node.child_by_field_name("declarator")
    name = (
        _declarator_name(node.child_by_field_name("name"), source)
        if node.type == "alias_declaration"
        # `typedef voidpf (*open_func) OF((voidpf opaque));` would be named after the macro.
        else None if is_macro_call_declarator(declarator)
        else _declarator_name(declarator, source)
    )
    return member_statement(
        node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids, name=name
    )


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    owner: str | None = None,
    name: str | None = None,
    alias: str | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return (classes, methods, statements) — all flat, linked by parentId. The class
    list is this class plus any nested (member) classes/structs parented to it. ``name``
    names the class instead of its tag (a ``typedef``'s name, see :func:`typedef_type`);
    ``alias`` is the tag when it differs."""
    if name is None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # anonymous struct/class — no name to emit; skip (honest gap)
            return [], [], []
        name = node_text(name_node, source)
    if node.child_by_field_name("body") is None:  # forward declaration (`class Foo;`) — not a
        return [], [], []                          # definition; emitting it fabricates a hollow node
    start, end = line_span(node)
    qualified = f"{owner}.{name}" if owner else name
    cid = disambiguate(class_id(path, qualified), seen_ids)
    extends, implements = _heritage(node, source)
    is_public_default = node.type in ("struct_specifier", "union_specifier")  # both default public

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        # A class body holds only declarations (no executable statements), so member
        # functions are extracted directly — nothing goes through extract_statements here.
        visibility = "public" if is_public_default else "private"
        for member in body.named_children:
            member = _unwrap_template(member)
            read = read_member(member, source)
            if read is None:
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if member.type == "access_specifier":
                visibility = node_text(member, source)
                continue
            if member.type in MACRO_TYPES:
                # A `#define` written inside the class body.
                if capture:
                    statements.append(
                        macro_statement(
                            member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                continue
            if member.type in TYPE_ALIAS_TYPES:
                # A member typedef/alias (`typedef int MemberHandle;`) declares a name on
                # the class and is otherwise captured nowhere.
                if capture:
                    statements.append(
                        type_alias_statement(
                            member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                continue
            spec = _nested_type_spec(member)
            if spec is not None:  # a nested type definition (class/struct/union/enum)
                if spec.type == "enum_specifier":
                    enum_cls, enum_stmts = build_enum(
                        spec, source, path, parent_id=cid, seen_ids=seen_ids,
                        capture=capture, limit=limit,
                    )
                    if enum_cls is not None:
                        nested_classes.append(enum_cls)
                        statements.extend(enum_stmts)
                else:
                    sub_classes, sub_methods, sub_statements = build_class(
                        spec, source, path,
                        parent_id=parent_id, seen_ids=seen_ids, capture=capture, limit=limit,
                        resolve=resolve, owner=qualified,
                    )
                    nested_classes.extend(sub_classes)
                    methods.extend(sub_methods)
                    statements.extend(sub_statements)
                declarator = member.child_by_field_name("declarator")
                if capture and member.type == "field_declaration" and declarator is not None:
                    # `struct node { … } root;` also declares a member, named even when the
                    # type is anonymous (`union { … } u;`).
                    statements.append(
                        member_statement(
                            member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                            name=_declarator_name(declarator, source),
                            decorators=decorators_of(member, source, read),
                        )
                    )
            elif member.type in _MEMBER_FN_TYPES:
                declarator = member.child_by_field_name("declarator")
                if function_declarator_of(declarator) is None or is_function_pointer(declarator):
                    # A member variable / constant, not a function — including a pointer to a
                    # function (`size_t (*read)(void *, size_t);`, C's method table). C++ class
                    # fields are otherwise captured nowhere, so emit each as a flat statement
                    # (its `text` — incl. any `= value` initializer — is queryable).
                    if capture and member.type == "field_declaration":
                        statements.append(
                            member_statement(
                                member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                                name=_declarator_name(member.child_by_field_name("declarator"), source),
                                decorators=decorators_of(member, source, read),
                            )
                        )
                    continue
                fn, fn_statements = build_member_function(
                    member, source, path,
                    parent_id=cid, class_name=name, id_owner=qualified, visibility=visibility,
                    seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve, read=read,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if fn.type == "constructor" and not ctor_params:
                    fd = function_declarator_of(member.child_by_field_name("declarator"))
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(
                            fd.child_by_field_name("parameters") if fd is not None else None, source
                        )
                    ]

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_AGGREGATE_TYPE.get(node.type, "class"),
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        startLine=start,
        endLine=end,
        alias=alias,
        decorators=_head_decorators(node, source),
    )
    return [cls, *nested_classes], methods, statements


def build_enum(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    name: str | None = None,
    alias: str | None = None,
) -> tuple[Class | None, list[Statement]]:
    """An ``enum_specifier`` → a ``Class`` of type ``enum`` plus one flat Statement per
    ``enumerator`` member (parented to the enum, gated by statement capture; the member's
    ``= value`` stays inside its ``text``). Anonymous enums and forward declarations (``enum
    Color;`` — no ``enumerator_list``) emit nothing (honest gap, no hollow node). ``enum
    class`` / ``enum struct`` are scoped enums, captured the same way (scoped-ness not stored).
    ``name`` names the enum instead of its tag (a ``typedef``'s name); ``alias`` is the tag
    when it differs."""
    if name is None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # anonymous enum — no name to emit
            return None, []
        name = node_text(name_node, source)
    body = next((c for c in node.named_children if c.type == "enumerator_list"), None)
    if body is None:
        return None, []  # forward declaration, not a definition
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="enum",
        startLine=start,
        endLine=end,
        alias=alias,
    )
    statements = (
        emit_enum_members(
            body, source, path,
            member_types={"enumerator"}, parent_id=cid, limit=limit, seen_ids=seen_ids,
        )
        if capture
        else []
    )
    return cls, statements


def _unwrap_template(node: Node) -> Node:
    """A ``template_declaration`` wraps its class/struct/function/alias — return the inner
    declaration so it is classified normally (else return the node unchanged). The alias
    case is the C++11 alias template, ``template<class T> using Ptr = T*;``."""
    if node.type != "template_declaration":
        return node
    inner = next(
        (c for c in node.named_children
         if c.type in (*_CLASS_TYPES, "function_definition", "field_declaration",
                       *TYPE_ALIAS_TYPES)),
        None,
    )
    return inner if inner is not None else node
