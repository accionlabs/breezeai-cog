"""The C parser, the C/C++ ownership evidence for ``.h``, and links between C and C++.

C is parsed with the C grammar and the C++ parser's extraction; the two languages share
``.h`` (decided per header by the ownership step) and read each other's index after their own.
Cross-file behaviour is tested end to end through ``analyze_repo`` at ``jobs`` 1 and 2, so the
ownership decisions and the related indexes are shown to reach the workers.
"""

from __future__ import annotations

import gzip
import json

import pytest

from breezeai_cog import analyze_repo
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.c.parser import CParser
from breezeai_cog.parsers.cfamily_common.imports import include_targets
from breezeai_cog.parsers.cpp.ownership import cpp_only_syntax
from breezeai_cog.parsers.cpp.parser import CppParser
from breezeai_cog.schemas import FileRecord

#: A C API header: its ``extern "C" {`` and ``}`` sit in separate ``#ifdef __cplusplus``
#: blocks, so neither grammar can pair the braces and the include guard parses with an error.
GUARDED_HEADER = """\
#ifndef LIST_H
#define LIST_H
#ifdef __cplusplus
extern "C" {
#endif
struct List { int size; };
enum color { RED, GREEN };
union num { int i; float f; };
void list_push(struct List *l, int x);
#ifdef __cplusplus
}
#endif
#endif
"""


def _parse(parser, tmp_path, rel: str, text: str) -> FileRecord:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return parser.parse_file(
        ParseContext(
            path=rel,
            abs_path=path,
            source=path.read_bytes(),
            repo_root=tmp_path,
            capture_statements=True,
        )
    )


def _repo(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def _capture(repo, jobs: int, **kw) -> dict[str, dict]:
    result = analyze_repo(repo, out=repo.parent / f"out-{repo.name}-{jobs}", jobs=jobs, **kw)
    with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]
    return {r["path"]: r for r in records if r.get("__type") != "projectMetaData"}


def _calls(record: dict, function: str) -> dict[str, str | None]:
    fn = next(f for f in record["functions"] if f["name"] == function)
    return {c["name"]: c.get("path") for c in fn.get("calls") or []}


# -- the C parser on one file -------------------------------------------------------------------


def test_c_file_language_and_structure(tmp_path) -> None:
    record = _parse(
        CParser(),
        tmp_path,
        "list.c",
        """\
#include <stdlib.h>
#include "list.h"
struct node { int value; };
static int helper(int v) { return v; }
int push(struct node *n, int x) { return helper(x); }
int old_style(a, b) int a; int b; { return a + b; }
""",
    )
    assert record.language == "c"
    assert record.externalImports == ["<stdlib.h>", "list.h"]  # no index: list.h unresolved
    assert [c.name for c in record.classes] == ["node"]
    assert [f.name for f in record.functions] == ["helper", "push", "old_style"]
    assert _calls(record.model_dump(), "push") == {"helper": "list.c"}  # same file


def test_cpp_keywords_are_identifiers_in_c(tmp_path) -> None:
    # C++ reads `new` and `class` as keywords and garbles this function; C reads it as written.
    record = _parse(
        CParser(), tmp_path, "k.c", "int f(int class) { int new = class + 1; return new; }\n"
    )
    assert [f.name for f in record.functions] == ["f"]
    assert [p.name for p in record.functions[0].params] == ["class"]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_guarded_c_api_header_is_captured(tmp_path, parser) -> None:
    record = _parse(parser, tmp_path, "list.h", GUARDED_HEADER)
    assert sorted(c.name for c in record.classes) == ["List", "color", "num"]


def test_functions_inside_extern_c_braces_are_captured(tmp_path) -> None:
    record = _parse(
        CppParser(),
        tmp_path,
        "api.cpp",
        """\
extern "C" {
int api_size(void) { return 1; }
int api_twice(void) { return api_size() * 2; }
}
""",
    )
    assert [f.name for f in record.functions] == ["api_size", "api_twice"]
    assert _calls(record.model_dump(), "api_twice") == {"api_size": "api.cpp"}


# -- ownership evidence -------------------------------------------------------------------------


def test_include_targets_are_local_includes_only() -> None:
    src = b'#include "a/list.h"\n  #  include "b.h"\n#include <stdio.h>\n// #include "c.h"\n'
    assert include_targets(src) == ["a/list.h", "b.h"]


@pytest.mark.parametrize(
    "src",
    [
        b"class Shape { int x; };",
        b"class Circle : public Shape {};",
        b"class Fwd;",
        b"class Leaf final {};",
        b"namespace geo { int x; }",
        b"namespace { int hidden; }",
        b"using namespace std;",
        b"template <typename T> T id(T v);",
        b"std::string name();",
        b'extern "C" int f(void);',
        b"#include <vector>\nint x;",
        # a header for both languages: C++ declarations inside its __cplusplus block
        b"int f(void);\n#ifdef __cplusplus\n#include <utility>\nstatic std::pair<int, int> g();\n#endif",
        b"#if defined(__cplusplus) && __cplusplus >= 201103L\nnamespace x {}\n#endif\nint f(void);",
        b"#ifndef __cplusplus\ntypedef int bool;\n#else\nclass B {};\n#endif\n",
    ],
)
def test_cpp_only_syntax_proves_cpp(src) -> None:
    assert cpp_only_syntax(src)


@pytest.mark.parametrize(
    "src",
    [
        GUARDED_HEADER.encode(),
        # a C header picking the C++ spelling of a library header for C++ includers
        b"#ifdef __cplusplus\n#include <cstddef>\n#else\n#include <stddef.h>\n#endif\nint f(void);",
        b"int class;\nint new = 1;",  # C++ keywords, valid C identifiers
        b"/* class Shape { }; */ int x;",  # a comment
        b'const char *s = "namespace x {";',  # a string
        b"@class Widget;",  # Objective-C forward declaration
        b"#include <stdio.h>\n#include <sys/types.h>",
        b"int a = b ? c : d;",
    ],
)
def test_c_syntax_proves_nothing(src) -> None:
    assert not cpp_only_syntax(src)


# -- whole repo: which language owns each header ------------------------------------------------


@pytest.mark.parametrize("jobs", [1, 2])
def test_header_language_follows_its_includers(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/list.h": GUARDED_HEADER,  # included from C and C++ → C
            "lib/list.c": '#include "list.h"\nvoid list_push(struct List *l, int x) {}\n',
            "app/view.h": "struct View { int w; };\n",  # included from C++ only → C++
            "app/main.cpp": '#include "lib/list.h"\n#include "view.h"\nint main() { return 0; }\n',
            "app/shape.h": "class Shape {};\n",  # C++ syntax → C++
        },
    )
    langs = {p: r["language"] for p, r in _capture(repo, jobs).items()}
    assert langs == {
        "lib/list.h": "c",
        "lib/list.c": "c",
        "app/view.h": "cpp",
        "app/main.cpp": "cpp",
        "app/shape.h": "cpp",
    }


def test_cpp_only_repo_headers_stay_cpp(tmp_path) -> None:
    repo = _repo(
        tmp_path / "r",
        {"a.h": "int f(void);\n", "unused.h": "int g(void);\n", "a.cpp": '#include "a.h"\n'},
    )
    assert {p: r["language"] for p, r in _capture(repo, 1).items()} == {
        "a.h": "cpp",
        "unused.h": "cpp",
        "a.cpp": "cpp",
    }


def test_language_filter_keeps_the_decision(tmp_path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/list.h": GUARDED_HEADER,
            "lib/list.c": '#include "list.h"\n',
            "app/view.h": "struct View { int w; };\n",
            "app/main.cpp": '#include "view.h"\n',
        },
    )
    assert sorted(_capture(repo, 1, languages=["c"])) == ["lib/list.c", "lib/list.h"]


# -- whole repo: links between C and C++ --------------------------------------------------------


@pytest.mark.parametrize("jobs", [1, 2])
def test_cpp_calls_and_includes_resolve_into_c(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/list.h": GUARDED_HEADER,
            "lib/list.c": '#include "list.h"\nvoid list_push(struct List *l, int x) {}\n',
            "app/main.cpp": '#include "list.h"\nint main() { struct List l; list_push(&l, 5); }\n',
        },
    )
    main = _capture(repo, jobs)["app/main.cpp"]
    assert main["importFiles"] == ["lib/list.h"]
    assert _calls(main, "main") == {"list_push": "lib/list.c"}


@pytest.mark.parametrize("jobs", [1, 2])
def test_c_calls_only_cpp_functions_with_c_linkage(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "log.cpp": 'extern "C" void log_c(const char *m) {}\nvoid log_cpp(const char *m) {}\n',
            "main.c": "void log_c(const char *m);\nvoid log_cpp(const char *m);\n"
            'int main(void) { log_c("a"); log_cpp("b"); return 0; }\n',
        },
    )
    calls = _calls(_capture(repo, jobs)["main.c"], "main")
    assert calls == {"log_c": "log.cpp", "log_cpp": None}  # C can't reach a C++-linkage function


def test_static_function_in_a_source_file_never_resolves_another_files_call(tmp_path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.c": "static int helper(void) { return 1; }\nint use_a(void) { return helper(); }\n",
            "b.c": "int use_b(void) { return helper(); }\n",  # some other helper, not a.c's
            "util.h": "static inline int clamp(int v) { return v; }\n",
            "c.c": '#include "util.h"\nint use_c(void) { return clamp(3); }\n',
        },
    )
    records = _capture(repo, 1)
    assert _calls(records["a.c"], "use_a") == {"helper": "a.c"}  # same file still resolves
    assert _calls(records["b.c"], "use_b") == {"helper": None}
    assert _calls(records["c.c"], "use_c") == {"clamp": "util.h"}  # a header's static is shared


def test_a_file_local_function_makes_its_name_ambiguous(tmp_path) -> None:
    # cog can't see which files a call can reach: with a file-local `run` in tool.cpp, the
    # header's `run` is not assumed to be the one main.cpp means.
    repo = _repo(
        tmp_path / "r",
        {
            "tool.cpp": "static void run() {}\n",
            "other/mean.hpp": "static void run() {}\n",
            "main.cpp": "int main() { run(); }\n",
        },
    )
    assert _calls(_capture(repo, 1)["main.cpp"], "main") == {"run": None}


def test_a_name_defined_in_both_languages_prefers_the_callers_own(tmp_path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "init.c": "void init(void) {}\n",
            "init.cpp": "void init() {}\n",
            "main.cpp": "void init();\nint main() { init(); }\n",
        },
    )
    assert _calls(_capture(repo, 1)["main.cpp"], "main") == {"init": "init.cpp"}


def test_a_header_name_in_both_languages_is_honest_null(tmp_path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "c/config.h": "int c_value;\n",
            "c/config_user.c": '#include "config.h"\n',
            "cpp/config.h": "class Config {};\n",
            "main.cpp": '#include "config.h"\n',
        },
    )
    main = _capture(repo, 1)["main.cpp"]
    assert main["importFiles"] == [] and main["externalImports"] == ["config.h"]


def test_a_header_for_both_languages_is_cpp_and_keeps_its_cpp_declarations(tmp_path) -> None:
    # C++ code in a __cplusplus block: only the C++ grammar reads the whole header.
    header = (
        "#pragma once\nstatic inline int clamp(int v) { return v; }\n"
        "#ifdef __cplusplus\n#include <utility>\n"
        "static std::pair<int, int> span(int n) { return {0, n}; }\n#endif\n"
    )
    repo = _repo(
        tmp_path / "r",
        {"common.h": header, "a.c": '#include "common.h"\n', "b.cpp": '#include "common.h"\n'},
    )
    record = _capture(repo, 1)["common.h"]
    assert record["language"] == "cpp"
    assert [f["name"] for f in record["functions"]] == ["clamp", "span"]


# -- pointers to functions are variables, not functions -----------------------------------------


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_pointer_to_a_function_is_a_field_not_a_method(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "rio.h",
        "struct rio {\n"
        "    size_t (*read)(struct rio *, void *buf, size_t len);\n"
        "    int **(*const lookup)(void);\n"
        "    int count;\n"
        "};\n"
        "int (*cmp)(int, int);\n",
    )
    assert record.functions == []
    fields = [(s.name, s.parentId) for s in record.statements if s.nodeType == "field_declaration"]
    assert fields == [("read", "rio.h#rio"), ("lookup", "rio.h#rio"), ("count", "rio.h#rio")]
    assert [(s.name, s.nodeType) for s in record.statements if s.parentId == "rio.h"] == [
        ("cmp", "declaration")
    ]


def test_a_member_function_with_a_parenthesized_name_is_still_a_method(tmp_path) -> None:
    record = _parse(CppParser(), tmp_path, "s.h", "struct S { int (f)(int); };\n")
    assert [f.name for f in record.functions] == ["f"]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_type_defined_with_a_variable_is_a_class(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "g.c",
        "struct point { int x; int y; } origin;\n"
        "static struct config { int level; } cfg = {1};\n"
        "struct { int a; } anon_var;\n"
        "union u { int a; float b; } uv;\n"
        "enum color { RED, GREEN } c;\n"
        "void f(void) { struct local { int a; } l; }\n",
    )
    assert [(c.name, c.type) for c in record.classes] == [
        ("point", "struct"), ("config", "struct"), ("u", "union"), ("color", "enum"),
    ]
    assert [(s.nodeType, s.name, s.parentId) for s in record.statements if s.parentId != "g.c#f@6"] == [
        ("field_declaration", "x", "g.c#point"),
        ("field_declaration", "y", "g.c#point"),
        ("declaration", "origin", "g.c"),
        ("field_declaration", "level", "g.c#config"),
        ("declaration", "cfg", "g.c"),
        ("declaration", "anon_var", "g.c"),
        ("field_declaration", "a", "g.c#u"),
        ("field_declaration", "b", "g.c#u"),
        ("declaration", "uv", "g.c"),
        ("enumerator", "RED", "g.c#color"),
        ("enumerator", "GREEN", "g.c#color"),
        ("declaration", "c", "g.c"),
    ]


def test_a_member_declared_with_its_nested_type_is_a_field(tmp_path) -> None:
    record = _parse(
        CppParser(),
        tmp_path,
        "trie.h",
        "struct trie {\n"
        "  struct node { int v; } root;\n"
        "  union { int i; float f; } u;\n"
        "  struct leaf { int w; };\n"
        "};\n",
    )
    assert [c.name for c in record.classes] == ["trie", "node", "leaf"]
    assert [(s.name, s.parentId) for s in record.statements] == [
        ("v", "trie.h#trie.node"), ("root", "trie.h#trie"), ("u", "trie.h#trie"),
        ("w", "trie.h#trie.leaf"),
    ]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_class_defined_with_a_variable_is_a_call_target(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path,
        {
            "lib/reg.hpp": "struct registry { int count() const { return 0; } } global_registry;\n",
            "app/main.cpp": (
                '#include "../lib/reg.hpp"\nint main() { registry r; return r.count(); }\n'
            ),
        },
    )
    assert _calls(_capture(repo, jobs)["app/main.cpp"], "main") == {"count": "lib/reg.hpp"}


# -- a C header adapting itself for C++ includers stays C ---------------------------------------


def test_a_header_adapting_itself_for_cpp_is_not_cpp() -> None:
    lzf_style = (
        b"#ifdef __cplusplus\n# include <cstring>\n# include <climits>\nusing namespace std;\n"
        b"#else\n# include <string.h>\n# include <limits.h>\n#endif\n"
    )
    assert not cpp_only_syntax(lzf_style)


# -- a library's own public headers are not taken for a vendored copy ---------------------------


def test_a_librarys_own_include_folder_is_captured(tmp_path) -> None:
    repo = _repo(
        tmp_path / "curl",
        {
            "include/curl/curl.h": "int curl_easy_init(void);\n",
            "lib/easy.c": '#include "curl/curl.h"\nint curl_easy_init(void) { return 0; }\n',
            "third_party/curl/include/curl/curl.h": "int vendored(void);\n",  # a vendored copy
            "lib/curl/old.c": "int old(void) { return 0; }\n",  # a vendored copy elsewhere
        },
    )
    assert sorted(_capture(repo, 1)) == ["include/curl/curl.h", "lib/easy.c"]


def test_an_unincluded_header_follows_its_folders_c_headers(tmp_path) -> None:
    # curl's include/curl/stdcheaders.h is included by nothing; its siblings are C.
    repo = _repo(
        tmp_path / "curl",
        {
            "include/curl/curl.h": "int curl_easy_init(void);\n",
            "include/curl/stdcheaders.h": "int strcasecmp(const char *, const char *);\n",
            "lib/easy.c": '#include "curl/curl.h"\n',
            "tests/fuzz.cpp": "int main() { return 0; }\n",  # a few C++ files: the repo is mixed
        },
    )
    assert _capture(repo, 1)["include/curl/stdcheaders.h"]["language"] == "c"


def test_the_index_parse_honours_the_parse_timeout(tmp_path) -> None:
    big = tmp_path / "big.c"
    big.write_text("".join(f"int f{i}(int a) {{ return a + {i}; }}\n" for i in range(20000)))
    assert "f1" in CParser().build_index(tmp_path, [big]).funcs
    # A parse past the limit skips the file (logged), as the parse stage does.
    assert CParser().build_index(tmp_path, [big], parse_timeout_micros=1).funcs == {}


# -- macros -------------------------------------------------------------------------------------


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_file_scope_macros_are_statements_but_include_guards_are_not(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "list.h",
        "#ifndef LIST_H\n#define LIST_H\n"
        "#define LIST_MAX 64\n"
        "#define MIN(a, b) ((a) < (b) ? (a) : (b))\n"
        "#define WITH_LOCKS\n"  # a flag, not a guard
        "#ifdef _WIN32\n#define SEP '\\\\'\n#endif\n"
        "#endif\n",
    )
    macros = [
        (s.nodeType, s.name, s.parentId)
        for s in record.statements
        if s.nodeType.startswith("preproc")
    ]
    assert macros == [
        ("preproc_def", "LIST_MAX", "list.h"),
        ("preproc_function_def", "MIN", "list.h"),
        ("preproc_def", "WITH_LOCKS", "list.h"),
        ("preproc_def", "SEP", "list.h"),
    ]
    assert all(
        s.semanticType is None for s in record.statements if s.nodeType.startswith("preproc")
    )


def test_macros_follow_the_statement_gate(tmp_path) -> None:
    path = tmp_path / "m.h"
    path.write_text("#define N 1\n")
    record = CParser().parse_file(
        ParseContext(path="m.h", abs_path=path, source=path.read_bytes(), repo_root=tmp_path)
    )
    assert record.statements == []


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_macro_is_a_statement_wherever_it_is_defined(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "m.c" if parser.name == "c" else "m.cpp",
        "#define TOP 1\n"
        "int f(int x) {\n"
        "#define LOCAL(a) ((a) + 1)\n"
        "    return LOCAL(x);\n"
        "}\n"
        "struct s {\n"
        "#define FIELD_MAX 8\n"
        "    int v[FIELD_MAX];\n"
        "};\n"
        "#define TWICE(a) \\\n"
        "    ((a) * 2)\n",
    )
    owner = {f.id: f.name for f in record.functions} | {c.id: c.name for c in record.classes}
    owner[record.id] = "<file>"
    macros = [
        (s.nodeType, s.name, s.startLine, s.endLine, owner[s.parentId])
        for s in record.statements if s.nodeType.startswith("preproc")
    ]
    assert sorted(macros, key=lambda m: m[2]) == [
        ("preproc_def", "TOP", 1, 1, "<file>"),
        ("preproc_function_def", "LOCAL", 3, 3, "f"),
        ("preproc_def", "FIELD_MAX", 7, 7, "s"),
        ("preproc_function_def", "TWICE", 10, 11, "<file>"),
    ]
    # A call to a macro stays unresolved: a macro is never a function.
    assert [(c.name, c.path) for c in record.functions[0].calls] == [("LOCAL", None)]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_macro_folded_into_a_keyword_type_is_not_the_return_type(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "t.c" if parser.name == "c" else "t.cpp",
        "unsigned long ZEXPORT crc(int c) { return c; }\n"
        "TARGET_ATTR\nlong long popcount(void *s) { return 0; }\n"
        "unsigned long long plain(void) { return 0; }\n",
    )
    assert {f.name: f.returnType for f in record.functions} == {
        "crc": "unsigned long",
        "popcount": "long long",
        "plain": "unsigned long long",
    }


def test_a_declaration_with_a_broken_declarator_is_not_named_after_a_parameter(tmp_path) -> None:
    # Old zlib declares prototypes through a macro: `OF((args))` expands to `(args)`.
    record = _parse(
        CParser(), tmp_path, "zlib.h",
        "int deflate OF((int strm, int flush));\n"
        "int deflateEnd(int strm);\n"
        # A type before the name, with a macro in front: the name is the word before `(`.
        "API_FUNC size_type table_len(table *t);\n"
        # An error in the initializer or after the name leaves the name intact.
        "struct item items[] = {\n  {1},\n#ifdef EXTRA\n  {2},\n#endif\n};\n"
        "oid_t mech_oid ALIGN8 = { 9, 0 };\n",
    )
    assert [s.name for s in record.statements if s.nodeType == "declaration"] == [
        "deflateEnd", "table_len", "items", "mech_oid",
    ]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_function_named_after_a_macro_before_its_name_gets_its_real_name(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "z.c" if parser.name == "c" else "z.cpp",
        "voidpf INTERNAL zalloc(void *opaque, unsigned n) { return 0; }\n"
        "int plain(int x) { return x; }\n"
        # A macro between `*` and the name leaves the name intact.
        "const char * EXPORT version(void) { return 0; }\n",
    )
    # `zalloc`: the grammar reads `INTERNAL` as the name, and the word right before `(` — the
    # real name — is recovered from the error; `returnType` is then unknown.
    assert [(f.name, f.returnType) for f in record.functions] == [
        ("zalloc", None), ("plain", "int"), ("version", "char"),
    ]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_macro_in_front_of_the_type_keeps_the_declaration(tmp_path, parser) -> None:
    # `API` is a macro the grammar takes for the type; the real type lands in an error. When
    # that is a type keyword (`int`), the type and the macro are certain: `returnType` is kept
    # and the macro is a decorator. After a type name (`API node *`), which word is the macro
    # can't be told: neither is recorded.
    record = _parse(
        parser,
        tmp_path,
        "api.c" if parser.name == "c" else "api.cpp",
        "API int size_of(list *l) { return 0; }\n"
        "API node *first(list *l) { return 0; }\n"
        "static INLINE int fast(int x) { return x; }\n"
        "API_FUNC size_type table_len(table *t);\n"
        "API void reset(list *l);\n"
        "static ATOMIC int counter = 0;\n"
        # Two declarators (a calling convention before the name): no name to show, skipped.
        "IMPORT errno_t __cdecl reopen(FILE **f);\n"
        "int use(list *l) { return size_of(l) + fast(1); }\n",
    )
    assert [
        (f.name, f.returnType, f.isStatic, [p.name for p in f.params], [d.name for d in f.decorators])
        for f in record.functions
    ] == [
        ("size_of", "int", None, ["l"], ["API"]),
        ("first", None, None, ["l"], []),
        ("fast", "int", True, ["x"], ["INLINE"]),
        ("use", "int", None, ["l"], []),
    ]
    assert [(c.name, c.path) for c in record.functions[-1].calls] == [
        ("size_of", record.path), ("fast", record.path),
    ]
    assert [
        (s.name, [d.name for d in s.decorators])
        for s in record.statements if s.nodeType == "declaration"
    ] == [("table_len", []), ("reset", ["API"]), ("counter", ["ATOMIC"])]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_macro_folded_into_unsigned_keeps_the_declaration(tmp_path, parser) -> None:
    # `LUALIB_API unsigned int f` reads as the type `LUALIB_API unsigned` with `int` in an error.
    record = _parse(
        parser,
        tmp_path,
        "seed.c",
        "LUALIB_API unsigned int make_seed(lua_State *L) { return 0; }\n"
        "local unsigned char *h2b(const char *hex) { return 0; }\n"
        "API signed char sign_of(int x);\n"
        # A type name, not a keyword, in the error: which word is the macro can't be told.
        "API unsigned seed_t other(void);\n",
    )
    assert [
        (f.name, f.returnType, [p.name for p in f.params], [d.name for d in f.decorators])
        for f in record.functions
    ] == [
        ("make_seed", "unsigned int", ["L"], ["LUALIB_API"]),
        ("h2b", "unsigned char", ["hex"], ["local"]),
    ]
    assert [
        (s.name, [d.name for d in s.decorators])
        for s in record.statements if s.nodeType == "declaration"
    ] == [("sign_of", ["API"])]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_macro_prefixed_function_is_a_call_target_in_other_files(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/api.h": "API int size_of(list *l);\nAPI_FUNC size_type table_len(table *t);\n",
            "lib/api.c": (
                '#include "api.h"\n'
                "API int size_of(list *l) { return 0; }\n"
                "API_FUNC size_type table_len(table *t) { return 0; }\n"
            ),
            "app/main.c": (
                '#include "api.h"\nint main(void) { return size_of(0) + table_len(0); }\n'
            ),
        },
    )
    main = _capture(repo, jobs)["app/main.c"]
    assert _calls(main, "main") == {"size_of": "lib/api.c", "table_len": "lib/api.c"}


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_row_of_macro_calls_is_not_a_function(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "ops.h": (
                "#define GEN(name) int name(int x) { return x; }\n"
                # A type written through a macro is still a type: this is a real function.
                "STACK_OF(cert) *certs(void) { return 0; }\n"
                # The row and the function after it read as one function named `GEN`; the
                # real function's name is lost in it, so nothing is emitted for either.
                "GEN(add)\nGEN(sub)\n"
                "static inline int real_fn(int x) { return x; }\n"
            ),
            "use.c": '#include "ops.h"\nint use(void) { return GEN(1) + certs() != 0; }\n',
        },
    )
    records = _capture(repo, jobs)
    assert [f["name"] for f in records["ops.h"]["functions"]] == ["certs"]
    assert _calls(records["use.c"], "use") == {"GEN": None, "certs": "ops.h"}


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_call_resolves_into_a_header_only_when_the_file_includes_it(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path,
        {
            "src/sds.h": "static inline int sdslen(const char *s) { return 0; }\n",
            "src/server.h": '#include "sds.h"\n',
            "src/server.c": '#include "server.h"\nint serve(void) { return sdslen("a"); }\n',
            "src/direct.c": '#include "sds.h"\nint direct(void) { return sdslen("a"); }\n',
            # Built against a vendored copy cog doesn't see: `<sds.h>` names no repo file.
            "src/cli.c": "#include <sds.h>\nint cli(void) { return sdslen(\"a\"); }\n",
            "src/none.c": "int none(void) { return sdslen(\"a\"); }\n",
            # A definition in a source file is linked by name, as before.
            "src/util.c": "int util_max(int a) { return a; }\n",
            "src/user.c": "int user(void) { return util_max(1); }\n",
        },
    )
    records = _capture(repo, jobs)
    assert _calls(records["src/server.c"], "serve") == {"sdslen": "src/sds.h"}  # transitive
    assert _calls(records["src/direct.c"], "direct") == {"sdslen": "src/sds.h"}
    assert _calls(records["src/cli.c"], "cli") == {"sdslen": None}
    assert _calls(records["src/none.c"], "none") == {"sdslen": None}
    assert _calls(records["src/user.c"], "user") == {"util_max": "src/util.c"}


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_call_to_an_inline_member_resolves_where_its_header_is_seen(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path,
        {
            "lib/cache.hpp": (
                "class Cache {\n"
                "public:\n"
                "  int hits() const { return n_; }\n"
                "  void touch() { n_ = hits() + 1; }\n"
                "  static Cache *global() { return nullptr; }\n"
                "  int n_ = 0;\n"
                "};\n"
                "class LruCache : public Cache { public: void evict() { touch(); } };\n"
            ),
            "app/main.cpp": (
                '#include "../lib/cache.hpp"\n'
                "int report(Cache &c) { c.touch(); return c.hits() + Cache::global()->n_; }\n"
            ),
            # Built against a copy cog doesn't see: `<cache.hpp>` names no repo file.
            "app/other.cpp": "#include <cache.hpp>\nint other(Cache &c) { return c.hits(); }\n",
        },
    )
    records = _capture(repo, jobs)
    assert _calls(records["app/main.cpp"], "report") == {
        "touch": "lib/cache.hpp", "hits": "lib/cache.hpp", "global": "lib/cache.hpp",
    }
    assert _calls(records["lib/cache.hpp"], "touch") == {"hits": "lib/cache.hpp"}
    assert _calls(records["lib/cache.hpp"], "evict") == {"touch": "lib/cache.hpp"}  # inherited
    assert _calls(records["app/other.cpp"], "other") == {"hits": None}


def test_a_variable_named_like_a_class_is_typed_not_read_as_the_class(tmp_path) -> None:
    repo = _repo(
        tmp_path,
        {
            "lib/arena.hpp": "struct arena { int parse() const; };\n",
            "lib/arena.cpp": '#include "arena.hpp"\nint arena::parse() const { return 0; }\n',
            "lib/parser.hpp": "class parser { public: int parse() { return 1; } };\n",
            "app/main.cpp": (
                '#include "../lib/arena.hpp"\n#include "../lib/parser.hpp"\n'
                "int run(const arena & parser) { return parser.parse(); }\n"
                "int run_auto() { static const auto parser = make(); return parser.parse(); }\n"
                "int run_cast(void *p) {\n"
                "  const auto * parser = static_cast<const arena *>(p); return parser->parse();\n"
                "}\n"
                "int run_struct() { struct arena parser; return parser.parse(); }\n"
            ),
        },
    )
    records = _capture(repo, 1)
    assert _calls(records["app/main.cpp"], "run") == {"parse": "lib/arena.cpp"}
    assert _calls(records["app/main.cpp"], "run_cast") == {"parse": "lib/arena.cpp"}
    assert _calls(records["app/main.cpp"], "run_struct") == {"parse": "lib/arena.cpp"}
    # Its type unknown, the local still isn't the class.
    assert _calls(records["app/main.cpp"], "run_auto") == {"make": None, "parse": None}


def test_the_include_closure_crosses_languages_and_cycles(tmp_path) -> None:
    repo = _repo(
        tmp_path,
        {
            "a.hpp": '#include "b.h"\nclass A {};\n',
            "b.h": '#include "a.hpp"\n#include "c.h"\n',
            "c.h": "static inline int c_len(void) { return 0; }\n",
            "main.cpp": '#include "a.hpp"\nint main() { return c_len(); }\n',
        },
    )
    assert _calls(_capture(repo, 1)["main.cpp"], "main") == {"c_len": "c.h"}


def test_a_header_sees_what_every_file_including_it_sees(tmp_path) -> None:
    # A header without includes of its own, compiled inside files that include its needs first.
    repo = _repo(
        tmp_path,
        {
            "base.h": "static inline int vec_add(int a) { return a; }\n",
            "other.h": "static inline int vec_mul(int a) { return a; }\n",
            "kernels.h": "static inline int kernel(int a) { return vec_add(a) + vec_mul(a); }\n",
            "a.c": '#include "base.h"\n#include "other.h"\n#include "kernels.h"\n',
            "b.c": '#include "base.h"\n#include "kernels.h"\n',
        },
    )
    # vec_mul is not visible from b.c, so it isn't proven for kernels.h.
    assert _calls(_capture(repo, 1)["kernels.h"], "kernel") == {
        "vec_add": "base.h",
        "vec_mul": None,
    }


def test_an_include_is_looked_up_next_to_the_including_file_first(tmp_path) -> None:
    repo = _repo(
        tmp_path,
        {
            "sycl/common.h": "static inline int set_device(int d) { return d; }\n",
            "zdnn/common.h": "static inline int zero_bias(int d) { return d; }\n",
            "sycl/main.c": '#include "common.h"\nint run(void) { return set_device(0); }\n',
        },
    )
    assert _calls(_capture(repo, 1)["sycl/main.c"], "run") == {"set_device": "sycl/common.h"}


# -- prototypes and static ----------------------------------------------------------------------


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_prototypes_are_declaration_statements_not_functions(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "list.h",
        "int list_len(const struct node *head);\n"
        "extern void list_free(struct node *head);\n"
        "int (list_parenthesized)(int n);\n"
        "static int forward(int);\n"
        # A macro the grammar takes for the type: misread as a function named `int`.
        "LUA_API int (lua_gettop) (lua_State *L);\n",
    )
    assert [(s.nodeType, s.name) for s in record.statements] == [
        ("declaration", "list_len"),
        ("declaration", "list_free"),
        ("declaration", "list_parenthesized"),
        ("declaration", "forward"),
        ("declaration", "lua_gettop"),
    ]
    assert record.functions == []


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_parenthesized_name_after_a_macro_and_a_keyword_type_is_recovered(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "lapi.c",
        "LUA_API void (lua_settop) (lua_State *L, int idx);\n"
        "LUA_API unsigned int (lua_rawlen) (lua_State *L, int idx);\n"
        # A type name, not a keyword, in front: it may be a type and a name-building macro.
        "LUALIB_API lua_Integer (luaL_len) (lua_State *L, int idx);\n"
        "LUA_API int (lua_gettop) (lua_State *L) { return 0; }\n"
        "LUA_API lua_Number (lua_tonumber) (lua_State *L) { return 0; }\n",
    )
    assert [
        (s.name, [d.name for d in s.decorators])
        for s in record.statements if s.nodeType == "declaration"
    ] == [("lua_settop", ["LUA_API"]), ("lua_rawlen", ["LUA_API"])]
    assert [
        (f.name, f.returnType, [d.name for d in f.decorators], [p.name for p in f.params])
        for f in record.functions
    ] == [("lua_gettop", "int", ["LUA_API"], ["L"])]


def test_a_parenthesized_member_name_after_a_macro_is_recovered(tmp_path) -> None:
    record = _parse(
        CppParser(),
        tmp_path,
        "k.h",
        "class K {\n"
        "  API int (f) (int a);\n"
        "  API unsigned long (g) (int a) { return 0; }\n"
        "  API size_type (h) (int a);\n"
        "};\n",
    )
    assert [
        (f.name, f.returnType, [d.name for d in f.decorators]) for f in record.functions
    ] == [("f", "int", ["API"]), ("g", "unsigned long", ["API"])]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_definition_with_a_parenthesized_name_is_named_without_them(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/lib.c": (
                "int (plain)(int a) { return a; }\n"
                "LUALIB_API lua_State *(luaL_newstate) (void) { return 0; }\n"
                # A pointer to a function is a variable, not a definition.
                "int (*hook)(int) = 0;\n"
            ),
            "app/main.c": "int main(void) { return plain(1) + (luaL_newstate() != 0); }\n",
        },
    )
    files = _capture(repo, jobs)
    assert [f["name"] for f in files["lib/lib.c"]["functions"]] == ["plain", "luaL_newstate"]
    assert _calls(files["app/main.c"], "main") == {
        "plain": "lib/lib.c", "luaL_newstate": "lib/lib.c",
    }


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_parenthesized_name_is_a_call_target(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "lib/lapi.h": "LUA_API int (lua_gettop) (lua_State *L);\n",
            "lib/lapi.c": (
                '#include "lapi.h"\n'
                "LUA_API int (lua_gettop) (lua_State *L) { return 0; }\n"
                "int lua_top1(lua_State *L) { return lua_gettop(L) + 1; }\n"
            ),
            "app/main.c": '#include "lapi.h"\nint main(void) { return lua_gettop(0); }\n',
        },
    )
    files = _capture(repo, jobs)
    assert _calls(files["app/main.c"], "main") == {"lua_gettop": "lib/lapi.c"}
    assert _calls(files["lib/lapi.c"], "lua_top1") == {"lua_gettop": "lib/lapi.c"}


def test_prototypes_follow_the_statement_gate(tmp_path) -> None:
    path = tmp_path / "p.h"
    path.write_text("int list_len(void);\n")
    record = CParser().parse_file(
        ParseContext(path="p.h", abs_path=path, source=path.read_bytes(), repo_root=tmp_path)
    )
    assert record.statements == []


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_static_free_function_is_static(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "a.c" if parser.name == "c" else "a.cpp",
        "static int helper(int x) { return x; }\n"
        "static inline int fast(int x) { return x; }\n"
        "int api(int x) { return helper(x); }\n",
    )
    assert {f.name: f.isStatic for f in record.functions} == {
        "helper": True,
        "fast": True,
        "api": None,
    }


def test_a_static_member_function_is_static_where_static_is_written(tmp_path) -> None:
    record = _parse(
        CppParser(),
        tmp_path,
        "pool.cpp",
        "class Pool {\n"
        "public:\n"
        "    static Pool& instance();\n"
        "    static int count() { return 0; }\n"
        "    template <typename U> static U conv(U u) { return u; }\n"
        "    int size() const;\n"
        "};\n"
        "Pool& Pool::instance() { static Pool p; return p; }\n"
        "int Pool::size() const { return 0; }\n",
    )
    assert [(f.name, f.startLine, f.isStatic) for f in record.functions] == [
        ("instance", 3, True),
        ("count", 4, True),
        ("conv", 5, True),
        ("size", 6, None),
        # C++ forbids `static` on an out-of-class definition, so it is not known there.
        ("instance", 8, None),
        ("size", 9, None),
    ]


# -- typedef'd types ----------------------------------------------------------------------------


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_typedef_that_defines_a_type_is_a_class_named_by_the_typedef(tmp_path, parser) -> None:
    record = _parse(
        parser,
        tmp_path,
        "kv.h",
        "typedef struct { int x, y; } point_t;\n"
        "typedef struct listNode { struct listNode *next; void *value; } listNode;\n"
        "typedef struct _kvstoreIterator { int didx; } kvstoreIterator;\n"
        "typedef enum { COLOR_RED, COLOR_GREEN } color_t;\n"
        "typedef union { int i; float f; } num_t;\n"
        # Not a definition, or not one plain name: stay type aliases.
        "typedef struct node node_t;\n"
        "typedef struct { int a; } *row_ptr;\n",
    )
    assert [(c.name, c.type, c.alias) for c in record.classes] == [
        ("point_t", "struct", None),
        ("listNode", "struct", None),  # the tag is the same name: no alias
        ("kvstoreIterator", "struct", "_kvstoreIterator"),
        ("color_t", "enum", None),
        ("num_t", "union", None),
    ]
    members = {
        c.name: [s.name for s in record.statements if s.parentId == c.id] for c in record.classes
    }
    assert members == {
        "point_t": ["x"],
        "listNode": ["next", "value"],  # a field typed `struct listNode *` is a field
        "kvstoreIterator": ["didx"],
        "color_t": ["COLOR_RED", "COLOR_GREEN"],
        "num_t": ["i", "f"],
    }
    assert [(s.nodeType, s.name) for s in record.statements if s.parentId == record.id] == [
        ("type_definition", "node_t"),
        ("type_definition", "row_ptr"),
    ]


def test_a_typedefd_struct_is_a_class_without_statement_capture(tmp_path) -> None:
    path = tmp_path / "p.h"
    path.write_text("typedef struct { int x; } point_t;\n")
    record = CParser().parse_file(
        ParseContext(path="p.h", abs_path=path, source=path.read_bytes(), repo_root=tmp_path)
    )
    assert [c.name for c in record.classes] == ["point_t"]
    assert record.statements == []


# -- calls through a variable -------------------------------------------------------------------


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_call_through_a_variable_never_links_to_a_function(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path,
        {
            "run.c": "int run(int x) { return x; }\nint cb(int x) { return x; }\n",
            "use.c": (
                "struct ops { int (*run)(int); };\n"
                "int via_member(struct ops *o) { return o->run(1); }\n"
                "int via_param(int (*cb)(int)) { return cb(3); }\n"
                "int via_local(void) { int (*cb)(int) = 0; return cb(4); }\n"
                # A local prototype declares a function, not a variable.
                "int via_prototype(void) { int cb(int); return cb(5); }\n"
                "int direct(void) { return run(6); }\n"
            ),
        },
    )
    use = _capture(repo, jobs)["use.c"]
    assert _calls(use, "via_member") == {"run": None}
    assert _calls(use, "via_param") == {"cb": None}
    assert _calls(use, "via_local") == {"cb": None}
    assert _calls(use, "via_prototype") == {"cb": "run.c"}
    assert _calls(use, "direct") == {"run": "run.c"}


def test_a_lambda_or_loop_variable_call_is_not_linked_in_cpp(tmp_path) -> None:
    repo = _repo(
        tmp_path,
        {
            "util.cpp": "int step(int x) { return x; }\nint each(int x) { return x; }\n",
            "main.cpp": (
                "#include <vector>\n"
                "int main() {\n"
                "  auto step = [](int x) { return x; };\n"
                "  std::vector<int (*)(int)> fns;\n"
                "  for (auto each : fns) { each(1); }\n"
                "  return step(2);\n"
                "}\n"
            ),
        },
    )
    assert _calls(_capture(repo, 1)["main.cpp"], "main") == {"each": None, "step": None}


def test_struct_with_a_macro_before_its_name_is_captured(tmp_path) -> None:
    # `struct API Tensor { … };` reads as a function named `Tensor` until the macro is
    # blanked and the file read again.
    rec = _parse(CParser(), tmp_path, "t.c", "struct API Tensor { int rank; };\nunion API U { int a; };\n")
    assert {c.name: c.type for c in rec.classes} == {"Tensor": "struct", "U": "union"}
    assert "rank" in {s.name for s in rec.statements}


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_attributes_and_certain_macros_are_decorators(tmp_path, parser) -> None:
    # Attributes the grammar parses become decorators with their arguments, in source order,
    # also one after the parameter list. A macro folded after `unsigned long` is certain too.
    record = _parse(
        parser,
        tmp_path,
        "attr.c" if parser.name == "c" else "attr.cpp",
        "static __attribute__((noinline, format(printf, 2, 3))) int logf2(const char *f, int a);\n"
        "void die(const char *m) __attribute__((noreturn));\n"
        "unsigned long ZEXPORT crc(unsigned long c) { return c; }\n"
        "uLong ZEXPORT adler(uLong a) { return a; }\n",
    )
    decorators = {
        s.name: [(d.name, d.args) for d in s.decorators]
        for s in [*record.statements, *record.functions] if s.name
    }
    assert decorators["logf2"] == [("noinline", []), ("format", ["printf", "2", "3"])]
    assert decorators["die"] == [("noreturn", [])]
    assert decorators["crc"] == [("ZEXPORT", [])]
    assert decorators["adler"] == []  # `uLong` or `ZEXPORT`: either could be the macro


def test_a_prototype_wrapped_in_a_parameter_macro_is_not_named_after_it(tmp_path) -> None:
    # Old-style headers wrap the parameter list in a macro: `int f OF((int a));`. With unnamed
    # parameters it reads as a function named `OF` whose one parameter has no type. Skipped:
    # no statement named after the macro, and no decorator taken from the real name.
    record = _parse(
        CParser(), tmp_path, "of.h",
        "local int gz_init OF((gz_statep));\n"
        "long call_seek OF((const def *p, int o));\n"
        "typedef voidpf (ZCALLBACK *open_func) OF((voidpf opaque, int mode));\n"
        "int kept(int a);\n",
    )
    assert [s.name for s in record.statements if s.nodeType == "declaration"] == ["kept"]
    # The typedef stays a statement (its text is the API), without a name taken from `OF`.
    assert [s.name for s in record.statements if s.nodeType == "type_definition"] == [None]


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_keyword_is_never_a_declared_name(tmp_path, parser) -> None:
    # A macro the grammar can't place runs into the next statement: `LOCALS` (no `;`) then
    # `if (a == 1) {…}` reads as a variable `if`, `OF((void))` as one named `void`. The
    # statements stay (their text is the code), without a name.
    record = _parse(
        parser,
        tmp_path,
        "kw.c" if parser.name == "c" else "kw.cpp",
        "void makefixed OF((void));\n"
        "void f(int a) {\n"
        "    LOCALS\n"
        "    if (a == 1) { g(); }\n"
        "    int count = 1;\n"
        "}\n",
    )
    names = {s.name for s in record.statements if s.nodeType == "declaration"}
    assert names & {"if", "void", "int"} == set()
    assert "count" in names


def test_a_word_reserved_only_in_cpp_is_a_name_in_c(tmp_path) -> None:
    record = _parse(CParser(), tmp_path, "new.c", "void f(void) { int new = 1; char *typename = 0; }\n")
    assert {"new", "typename"} <= {s.name for s in record.statements}


#: A function whose braces two `#ifdef` blocks split: one opens a brace (`} else {`), a later one
#: closes it. The compiler sees both or neither; the grammar reads both and can't match them.
SPLIT_BRACES = """static void dump(int kind, int a) {
  switch (kind) {
  case 1: {
    if (a) {
      put(1);
    } else {
#ifdef USE_DEC
      const char *lit = literal();
      if (lit) {
        put_lit(lit);
      } else {
#endif
        double d = value();
        if (d != d) {
          put(2);
        } else {
          put(3);
        }
      }
#ifdef USE_DEC
    }
#endif
    break;
  }
  default:
    put(4);
  }
}

#ifdef _WIN32
int platform(void) { return 1; }
#else
int platform(void) { return 2; }
#endif

void after_one(int y) { use(y); }
void after_two(int z) { dump(z, 1); }
"""


@pytest.mark.parametrize("parser", [CParser(), CppParser()], ids=["c", "cpp"])
def test_a_function_whose_braces_a_conditional_splits_ends_where_it_ends(tmp_path, parser) -> None:
    # Read once more with each conditional resolved to its first branch, the function ends at
    # its own `}`: the functions after it are captured, their calls no longer credited to it.
    # Those come from that second reading, so of `platform` only the first branch's is there
    # (before, neither was: both sat inside `dump`).
    record = _parse(parser, tmp_path, "split.c" if parser.name == "c" else "split.cpp", SPLIT_BRACES)
    fns = [(f.name, f.startLine, f.endLine) for f in record.functions]
    assert fns == [("dump", 1, 28), ("platform", 31, 31), ("after_one", 36, 36), ("after_two", 37, 37)]
    dump = record.functions[0]
    assert {c.name for c in dump.calls} == {"put", "literal", "put_lit", "value"}
    assert [(c.name, c.path) for c in record.functions[-1].calls] == [("dump", record.path)]


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_function_after_split_braces_is_a_call_target(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {"src/split.c": SPLIT_BRACES, "src/use.c": "void go(void) { after_one(1); }\n"},
    )
    assert _calls(_capture(repo, jobs)["src/use.c"], "go") == {"after_one": "src/split.c"}


def test_a_conditional_outside_a_split_function_keeps_both_branches(tmp_path) -> None:
    # Only the span a split function wrongly covered is read from the second parse: a function
    # defined in both branches elsewhere in the file keeps both definitions.
    text = (
        "#ifdef _WIN32\nint platform(void) { return 1; }\n#else\nint platform(void) { return 2; }\n#endif\n"
        + SPLIT_BRACES.split("#ifdef _WIN32")[0]
    )
    record = _parse(CParser(), tmp_path, "both.c", text)
    assert [f.name for f in record.functions] == ["platform", "platform", "dump"]
    assert record.functions[2].endLine == 33


def test_a_class_after_split_braces_is_captured(tmp_path) -> None:
    # Everything in the span the split function covered comes from the second parse: a class
    # with its methods, inside its namespace, as well as functions.
    text = SPLIT_BRACES.split("#ifdef _WIN32")[0] + (
        "namespace ui {\n"
        "    struct spinner {\n"
        "        spinner() { start(); }\n"
        "        ~spinner() { stop(); }\n"
        "    };\n"
        "    static void show(int m) { log(m); }\n"
        "}\n"
    )
    record = _parse(CppParser(), tmp_path, "ui.cpp", text)
    assert [(c.name, c.startLine, c.endLine) for c in record.classes] == [("spinner", 31, 34)]
    assert [(f.name, f.type) for f in record.functions] == [
        ("dump", "function"), ("spinner", "constructor"), ("~spinner", "method"),
        ("show", "function"),
    ]
