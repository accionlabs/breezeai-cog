"""Comment capture across all languages (BREEZEAI-974).

Every language parser runs the shared comment pass (``parsers/comments_common``) that turns
source comments into flat ``Statement`` records (``nodeType="comment"``,
``semanticType="comment"``), scoped by the binding rule and deduped against statements whose
``text`` already contains the comment. These tests exercise: capture + gating, per-language
comment node types, the binding rule (bind-ahead across decorators/annotations → containment
→ file), consecutive-line merge, the dedup rule, and Python docstrings.
"""

from __future__ import annotations

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
