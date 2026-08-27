"""Comment capture across all languages (BREEZEAI-974).

Every language parser runs the shared comment pass (``parsers/comments_common``) that turns
source comments into flat ``Statement`` records (``nodeType="comment"``,
``semanticType="comment"``), scoped by the binding rule and deduped against statements whose
``text`` already contains the comment. These tests exercise: capture + gating, per-language
comment node types, the binding rule (bind-ahead across decorators/annotations → containment
→ file), consecutive-line merge, the dedup rule, and Python docstrings.
"""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cpp.parser import CppParser
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.groovy.parser import GroovyParser
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.parsers.kotlin.parser import KotlinParser
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.vb.parser import VbParser


def _parse(tmp_path, parser, filename: str, src: str, *, capture: bool = True):
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename, abs_path=p, source=src.encode(), repo_root=tmp_path,
        capture_statements=capture, statement_text_limit=1000,
    )
    return parser.parse_file(ctx)


def _stmt_texts(rec):
    """text of every non-comment statement."""
    return [s.text for s in rec.statements if s.semanticType != "comment"]


def test_inline_comment_folds_into_statement_text(tmp_path) -> None:
    """A same-line trailing comment is folded into its statement's ``text`` (not a separate
    comment node); an own-line comment stays a first-class comment node."""
    src = (
        "class A {\n"
        "    // fields section\n"                 # L2 own line -> comment node
        "    int count = 0; // instance count\n"  # L3 trailing -> folded into field text
        "    void run() {\n"
        "        if (count > 0) { // positive\n"   # L5 control-flow header trailing -> folded
        "            count--; // decrement\n"       # L6 trailing -> folded
        "        }\n"
        "        return; // done\n"                 # L8 trailing -> folded
        "    }\n"
        "}\n"
    )
    rec = _parse(tmp_path, JavaParser(), "A.java", src)
    texts = _stmt_texts(rec)
    comment_texts = [t for t, *_ in _comments(rec)]

    # trailing comments live inside statement text …
    assert "int count = 0; // instance count" in texts
    assert "if (count > 0) { // positive" in texts
    assert "count--; // decrement" in texts
    assert "return; // done" in texts
    # … and are NOT also emitted as their own comment nodes
    for frag in ("instance count", "positive", "decrement", "done"):
        assert not any(frag in c for c in comment_texts), frag
    # the own-line comment IS a comment node, not folded
    assert "// fields section" in comment_texts


def test_last_enum_member_trailing_doc_folds(tmp_path) -> None:
    """Trailing doc on the last enum member (which trails a ``;`` marker node) still folds."""
    src = "enum S {\n  A(1),  // first\n  B(2);  // second\n}\n"
    rec = _parse(tmp_path, JavaParser(), "S.java", src)
    texts = _stmt_texts(rec)
    assert "A(1),  // first" in texts
    assert "B(2);  // second" in texts
    assert not any("first" in c or "second" in c for c, *_ in _comments(rec))


def _comments(rec):
    """(text, parentName) for each comment statement, using a scope-id → name map."""
    names = {rec.id: "<FILE>"}
    names.update({f.id: f.name for f in rec.functions})
    names.update({c.id: c.name for c in rec.classes})
    return [(s.text, names.get(s.parentId, "?"), s.startLine, s.endLine)
            for s in rec.statements if s.semanticType == "comment"]


# --- Java: split node types (line_comment/block_comment), annotation bind-ahead, dedup -----

def test_java_comment_capture(tmp_path) -> None:
    src = (
        "// file header\n"                       # L1 -> class A (bind-ahead)
        "class A {\n"
        "    /** doc for m */\n"                  # L3 -> m (across @Override)
        "    @Override\n"
        "    void m() {\n"
        "        // in body\n"                     # L6+7 merged -> m
        "        // second line\n"
        "        int x = call(\n"
        "            1 // inside call\n"           # L9 -> deduped (inside multi-line stmt)
        "        );\n"
        "    }\n"
        "}\n"
    )
    rec = _parse(tmp_path, JavaParser(), "A.java", src)
    got = _comments(rec)
    texts = {t for t, *_ in got}
    assert "// file header" in texts
    assert ("/** doc for m */", "m", 3, 3) in got                    # doc binds across annotation
    assert ("// in body\n        // second line", "m", 6, 7) in got  # consecutive merged
    assert not any("inside call" in t for t, *_ in got)              # deduped

    # Gating: no comments when capture is off.
    assert not _comments(_parse(tmp_path, JavaParser(), "A.java", src, capture=False))


# --- Kotlin: multiline_comment node type; previously-unreachable file scope ----------------

def test_kotlin_comment_capture(tmp_path) -> None:
    src = (
        "// header\n"
        "class A {\n"
        "    /* block */\n"
        "    fun m() {\n"
        "        // body note\n"
        "        val x = 1\n"
        "    }\n"
        "}\n"
    )
    got = _comments(_parse(tmp_path, KotlinParser(), "A.kt", src))
    texts = {t for t, *_ in got}
    assert "// header" in texts
    assert "/* block */" in texts                                    # multiline_comment captured
    assert ("// body note", "m", 5, 5) in got


# --- Groovy: groovydoc_comment node type --------------------------------------------------

def test_groovy_comment_capture(tmp_path) -> None:
    src = (
        "// header\n"
        "class A {\n"
        "    /** groovydoc */\n"
        "    void m() {\n"
        "        // body note\n"
        "        def x = 1\n"
        "    }\n"
        "}\n"
    )
    got = _comments(_parse(tmp_path, GroovyParser(), "A.groovy", src))
    assert "/** groovydoc */" in {t for t, *_ in got}                # groovydoc_comment captured
    assert ("// body note", "m", 5, 5) in got


# --- C++: file-root AND class-body scopes (no extract_statements there — pass reaches them) -

def test_cpp_comment_capture(tmp_path) -> None:
    src = (
        "// header\n"                             # L1 file/class scope
        "class A {\n"
        "  // field doc\n"                        # L3 -> class A (class-body: no old seam)
        "  int k = 0;\n"
        "  void m() {\n"
        "    // body note\n"                       # L6 -> m
        "    int x = 0;\n"
        "  }\n"
        "};\n"
    )
    got = _comments(_parse(tmp_path, CppParser(), "a.cpp", src))
    assert ("// field doc", "A", 3, 3) in got                        # class-body comment reachable
    assert ("// body note", "m", 6, 6) in got


# --- C# ------------------------------------------------------------------------------------

def test_csharp_comment_capture(tmp_path) -> None:
    src = (
        "// header\n"
        "class A {\n"
        "    // doc for M\n"
        "    void M() {\n"
        "        // body note\n"
        "        var x = 1;\n"
        "    }\n"
        "}\n"
    )
    got = _comments(_parse(tmp_path, CSharpParser(), "A.cs", src))
    assert ("// body note", "M", 5, 5) in got
    assert "// header" in {t for t, *_ in got}


# --- VB: apostrophe comments; file scope (no extract_statements at file scope) -------------

def test_vb_comment_capture(tmp_path) -> None:
    src = (
        "' header comment\n"
        "Class A\n"
        "    Sub M()\n"
        "        ' body note\n"
        "        Dim x = 1\n"
        "    End Sub\n"
        "End Class\n"
    )
    got = _comments(_parse(tmp_path, VbParser(), "A.vb", src))
    assert ("' body note", "M", 4, 4) in got
    assert "' header comment" in {t for t, *_ in got}


# --- TypeScript ---------------------------------------------------------------------------

def test_typescript_comment_capture(tmp_path) -> None:
    src = (
        "// header\n"
        "class A {\n"
        "  // doc for m\n"
        "  m() {\n"
        "    // body note\n"
        "    const x = 1; // trailing -> deduped\n"
        "  }\n"
        "}\n"
    )
    got = _comments(_parse(tmp_path, TypeScriptParser(), "a.ts", src))
    assert ("// body note", "m", 5, 5) in got
    assert not any("trailing" in t for t, *_ in got)                 # deduped (in stmt text)


# --- Python: docstrings tagged as comments; merge; control-flow-body comment kept ----------

def test_python_comment_capture(tmp_path) -> None:
    src = (
        "class E:\n"
        '    """class doc."""\n'                   # L2 docstring -> E, tagged comment
        "    # preamble a\n"                        # L3+4 merged -> E
        "    # preamble b\n"
        "    total = 0\n"
        "    def m(self):\n"
        '        """method doc."""\n'               # L7 docstring -> m
        "        if total > 0:\n"
        "            # inside if body\n"             # L9 -> m (control-flow not absorbing)
        "            return 1\n"
    )
    rec = _parse(tmp_path, PythonParser(), "e.py", src)
    got = _comments(rec)
    assert ('"""class doc."""', "E", 2, 2) in got                    # docstring -> class, as comment
    assert ("# preamble a\n    # preamble b", "E", 3, 4) in got      # merged
    assert ('"""method doc."""', "m", 7, 7) in got                  # docstring -> method
    assert ("# inside if body", "m", 9, 9) in got                    # if-body comment survives

    # Gating.
    assert not _comments(_parse(tmp_path, PythonParser(), "e.py", src, capture=False))


# --- Multi-language "nothing dropped, nothing duplicated" fixture (BREEZEAI-974 AC) --------
#
# Every comment carries a unique marker. The invariant across ALL languages: each marker is
# accounted for exactly once — either a standalone comment node OR folded into one
# statement's text, never both (no duplication), never as two nodes, never missing (no drop).

_CLIKE = (
    "// k_hdr\n"
    "class A {{\n"
    "    // k_body\n"
    "    {ret} m() {{\n"
    "        int x = 1; // k_trail\n"
    "        // k_m1\n"
    "        // k_m2\n"
    "        int y = 2;\n"
    "    }}\n"
    "}}{tail}\n"
)
_CLIKE_MARKERS = ["k_hdr", "k_body", "k_trail", "k_m1", "k_m2"]

_COMMENT_FIXTURES = [
    (JavaParser(),       "A.java",   _CLIKE.format(ret="void", tail=""), _CLIKE_MARKERS),
    (CSharpParser(),     "A.cs",     _CLIKE.format(ret="void", tail=""), _CLIKE_MARKERS),
    (CppParser(),        "a.cpp",    _CLIKE.format(ret="int", tail=";"), _CLIKE_MARKERS),
    (GroovyParser(),     "A.groovy", _CLIKE.format(ret="void", tail=""), _CLIKE_MARKERS),
    (
        TypeScriptParser(), "a.ts",
        "// k_hdr\nclass A {\n  // k_body\n  m() {\n    let x = 1; // k_trail\n"
        "    // k_m1\n    // k_m2\n    let y = 2;\n  }\n}\n",
        _CLIKE_MARKERS,
    ),
    (
        KotlinParser(), "A.kt",
        "// k_hdr\nclass A {\n  // k_body\n  fun m() {\n    val x = 1 // k_trail\n"
        "    // k_m1\n    // k_m2\n    val y = 2\n  }\n}\n",
        _CLIKE_MARKERS,
    ),
    (
        VbParser(), "A.vb",
        "' k_hdr\nClass A\n    Sub M()\n        Dim x = 1 ' k_trail\n"
        "        ' k_m1\n        ' k_m2\n        Dim y = 2\n    End Sub\nEnd Class\n",
        ["k_hdr", "k_trail", "k_m1", "k_m2"],
    ),
    (
        PythonParser(), "a.py",
        "# k_hdr\nclass A:\n    \"\"\"k_doc.\"\"\"\n    def m(self):\n        x = 1  # k_trail\n"
        "        # k_m1\n        # k_m2\n        y = 2\n",
        ["k_hdr", "k_doc", "k_trail", "k_m1", "k_m2"],
    ),
]


@pytest.mark.parametrize(
    "parser,filename,src,markers", _COMMENT_FIXTURES,
    ids=[fx[1] for fx in _COMMENT_FIXTURES],
)
def test_no_comment_dropped_or_duplicated(tmp_path, parser, filename, src, markers) -> None:
    rec = _parse(tmp_path, parser, filename, src)
    node_texts = [s.text or "" for s in rec.statements if s.semanticType == "comment"]
    carrier_texts = [s.text or "" for s in rec.statements if s.semanticType != "comment"]
    for m in markers:
        node_hits = sum(1 for t in node_texts if m in t)
        in_carrier = any(m in t for t in carrier_texts)
        assert node_hits <= 1, f"{filename}: {m} emitted as {node_hits} comment nodes"
        assert node_hits + int(in_carrier) >= 1, f"{filename}: {m} dropped"
        assert not (node_hits and in_carrier), f"{filename}: {m} both a node and folded"
