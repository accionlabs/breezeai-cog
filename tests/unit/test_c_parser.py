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
from breezeai_cog.parsers.cpp.ownership import cpp_only_syntax, include_targets
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
