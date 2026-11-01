"""The ownership step (``core/ownership.py``) and how the pipeline uses its decisions.

Two stand-in languages share the ``.sh2`` extension the way C and C++ share ``.h``: ``lower``
(own files ``.lo``) and ``upper`` (own files ``.up``). ``upper`` can reference ``lower`` files,
``.up`` content containing ``UPPER_ONLY`` proves ``upper``, and both reference a file with a
``use "x"`` line. Nothing here depends on the real C and C++ parsers.
"""

from __future__ import annotations

import re

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.core import pipeline, registry
from breezeai_cog.core.ownership import competing_languages, resolve_owners, shared_languages
from breezeai_cog.core.scanner import ScanEntry
from breezeai_cog.parsers.base import BaseParser, Ownership, ParseContext
from breezeai_cog.schemas import FileRecord

_USE = re.compile(rb'^\s*use\s+"([^"]+)"', re.M)


def _uses(source: bytes) -> list[str]:
    return [m.decode() for m in _USE.findall(source)]


def _upper_only(source: bytes) -> bool:
    return b"UPPER_ONLY" in source


class Lower(BaseParser):
    name = "lower"
    extensions = (".lo", ".sh2")
    ownership = Ownership(references=_uses)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language=self.name, loc=1)


class Upper(BaseParser):
    name = "upper"
    extensions = (".up", ".sh2")
    ownership = Ownership(
        proves=_upper_only, references=_uses, includes=("lower",), default_for=(".sh2",)
    )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language=self.name, loc=1)


class UpperFramework(Upper):
    name = "upper-fw"
    priority = 10

    def claims(self, path: str, source: bytes) -> bool:
        return b"FRAMEWORK" in source


class LowerSubclass(Lower):
    """A different base language that happens to subclass ``Lower`` (as C subclasses C++)."""

    name = "lower-sub"
    extensions = (".ls",)


@pytest.fixture(autouse=True)
def _languages():
    registry.clear()
    for parser in (Lower(), Upper(), UpperFramework()):
        registry.register(parser)
    yield
    registry.clear()


def _repo(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def _decide(tmp_path, files: dict[str, str]) -> dict[str, tuple[str, str]]:
    repo = _repo(tmp_path, files)
    entries = [ScanEntry(path=p, language=registry.base_parser_for(p).name) for p in files]
    return {p: (d.language, d.rule) for p, d in resolve_owners(repo, entries).items()}


# -- which files are shared ---------------------------------------------------------------------


def test_only_an_extension_two_languages_register_is_shared() -> None:
    assert shared_languages("a.sh2") == ("lower", "upper")
    assert shared_languages("a.lo") == ()
    assert shared_languages("a.up") == ()  # upper-fw sits above upper, it is not a language


def test_competing_languages() -> None:
    assert competing_languages({"lower"}) == {"upper"}
    assert competing_languages({"upper"}) == {"lower"}


def test_no_shared_file_no_work(tmp_path) -> None:
    assert _decide(tmp_path, {"a.lo": "", "b.up": ""}) == {}


# -- rule 1: content ----------------------------------------------------------------------------


def test_content_only_one_language_allows(tmp_path) -> None:
    decided = _decide(tmp_path, {"x.sh2": "UPPER_ONLY", "a.lo": 'use "x.sh2"'})
    assert decided == {"x.sh2": ("upper", "content")}  # content wins over a lower includer


# -- rule 2: references -------------------------------------------------------------------------


def test_referenced_from_one_language(tmp_path) -> None:
    assert _decide(tmp_path, {"x.sh2": "", "a.lo": 'use "x.sh2"'}) == {
        "x.sh2": ("lower", "references")
    }
    assert _decide(tmp_path / "2", {"x.sh2": "", "a.up": 'use "x.sh2"'}) == {
        "x.sh2": ("upper", "references")
    }


def test_referenced_from_both_goes_to_the_language_both_can_include(tmp_path) -> None:
    decided = _decide(
        tmp_path, {"lib/x.sh2": "", "lib/a.lo": 'use "x.sh2"', "app/b.up": 'use "lib/x.sh2"'}
    )
    assert decided == {"lib/x.sh2": ("lower", "references")}


def test_references_are_followed_back_through_undecided_shared_files(tmp_path) -> None:
    decided = _decide(tmp_path, {"x.sh2": "", "api.sh2": 'use "x.sh2"', "main.up": 'use "api.sh2"'})
    assert decided == {"x.sh2": ("upper", "references"), "api.sh2": ("upper", "references")}


def test_a_shared_file_decided_by_content_counts_as_its_language(tmp_path) -> None:
    decided = _decide(tmp_path, {"x.sh2": "", "api.sh2": 'UPPER_ONLY\nuse "x.sh2"', "a.lo": ""})
    assert decided["x.sh2"] == ("upper", "references")


def test_a_reference_matching_two_files_is_not_evidence(tmp_path) -> None:
    decided = _decide(
        tmp_path, {"one/x.sh2": "", "two/x.sh2": "", "main.lo": 'use "x.sh2"', "other.up": ""}
    )
    # 'x.sh2' names neither (no local file, two by suffix): falls through to the default
    assert decided["one/x.sh2"] == ("upper", "default")


def test_a_relative_reference_resolves_from_the_referrer(tmp_path) -> None:
    decided = _decide(
        tmp_path, {"one/x.sh2": "", "two/x.sh2": "", "one/main.lo": 'use "x.sh2"', "other.up": ""}
    )
    assert decided["one/x.sh2"] == ("lower", "references")


# -- rule 3: neighbours, then repo --------------------------------------------------------------


def test_unreferenced_file_takes_its_folders_language(tmp_path) -> None:
    decided = _decide(tmp_path, {"lib/x.sh2": "", "lib/a.lo": "", "app/b.up": ""})
    assert decided == {"lib/x.sh2": ("lower", "neighbours")}


def test_unreferenced_file_takes_the_language_its_folders_shared_files_were_decided(
    tmp_path,
) -> None:
    # inc/ holds shared files only: inc/a.sh2 is decided by its includer, so inc/b.sh2 follows it
    # even though the repo has both languages.
    decided = _decide(
        tmp_path, {"inc/a.sh2": "", "inc/b.sh2": "", "src/x.lo": 'use "a.sh2"', "app/y.up": ""}
    )
    assert decided == {"inc/a.sh2": ("lower", "references"), "inc/b.sh2": ("lower", "neighbours")}


def test_unreferenced_file_takes_the_repos_language(tmp_path) -> None:
    decided = _decide(tmp_path, {"inc/x.sh2": "", "src/a.lo": ""})
    assert decided == {"inc/x.sh2": ("lower", "repo")}


# -- rule 4: default ----------------------------------------------------------------------------


def test_nothing_decides_it_the_declared_default(tmp_path) -> None:
    decided = _decide(tmp_path, {"inc/x.sh2": "", "a/a.lo": "", "b/b.up": ""})
    assert decided == {"inc/x.sh2": ("upper", "default")}


def test_no_declared_default_first_registered(tmp_path) -> None:
    registry.clear()
    registry.register(Lower())
    registry.register(type("NoDefault", (Upper,), {"ownership": Ownership()})())
    decided = _decide(tmp_path, {"x.sh2": ""})
    assert decided == {"x.sh2": ("lower", "default")}  # what the scan chose before the step


# -- registry: a decided language selects only its own parsers ----------------------------------


def test_select_honours_the_decided_language() -> None:
    assert registry.select("x.sh2", b"FRAMEWORK", language="lower").name == "lower"
    assert registry.select("x.sh2", b"FRAMEWORK", language="upper").name == "upper-fw"
    assert registry.select("x.sh2", b"", language="upper").name == "upper"
    assert registry.base_parser_for("x.sh2", "upper").name == "upper"
    assert registry.base_parser_for("x.sh2").name == "lower"  # undecided: first registered


def test_another_base_language_is_never_a_framework_of_its_superclass() -> None:
    registry.register(LowerSubclass())
    assert registry.select("x.ls", b"", language="lower-sub").name == "lower-sub"
    assert registry.select("x.sh2", b"", language="lower").name == "lower"


# -- pipeline -----------------------------------------------------------------------------------


def _scan(repo, **settings) -> dict[str, str]:
    return {
        e.path: e.language for e in pipeline._scan_entries(repo, Settings(repo=repo, **settings))
    }


def test_scan_labels_shared_files_with_the_decided_language(tmp_path) -> None:
    repo = _repo(tmp_path, {"lib/x.sh2": "", "lib/a.lo": 'use "x.sh2"', "app/b.up": ""})
    assert _scan(repo) == {"lib/x.sh2": "lower", "lib/a.lo": "lower", "app/b.up": "upper"}


def test_language_filter_still_reads_competing_files_as_evidence(tmp_path) -> None:
    repo = _repo(tmp_path, {"x.sh2": "", "y.sh2": "", "a.up": 'use "x.sh2"', "b.lo": 'use "y.sh2"'})
    assert _scan(repo, languages=["lower"]) == {"y.sh2": "lower", "b.lo": "lower"}
    assert _scan(repo, languages=["upper"]) == {"x.sh2": "upper", "a.up": "upper"}


class _Index:
    def __init__(self, name: str) -> None:
        self.name = name


class IndexedLower(Lower):
    def build_index(self, repo_root, files, jobs=1):
        return _Index("lower")


class IndexedUpper(Upper):
    related_languages = ("lower",)

    def build_index(self, repo_root, files, jobs=1):
        return _Index("upper")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        related = ",".join(f"{k}={v.name}" for k, v in sorted(ctx.related_indexes.items()))
        own = ctx.resolution_index.name if ctx.resolution_index else "-"
        return FileRecord(
            id=ctx.path,
            path=ctx.path,
            type="code",
            language=self.name,
            loc=1,
            externalImports=[f"own={own}", related],
        )


def test_parsers_get_their_own_index_and_related_indexes(tmp_path) -> None:
    from breezeai_cog import iter_file_records

    registry.clear()
    registry.register(IndexedLower())
    registry.register(IndexedUpper())
    repo = _repo(tmp_path, {"x.sh2": "UPPER_ONLY", "a.up": "", "b.lo": ""})
    records = {r.path: r for r in iter_file_records(repo)}  # sequential, in-process
    assert records["x.sh2"].language == "upper"  # parsed by the decided language's parser
    assert records["x.sh2"].externalImports == ["own=upper", "lower=lower"]
    assert records["a.up"].externalImports == ["own=upper", "lower=lower"]
    assert records["b.lo"].language == "lower"


class TimedLower(Lower):
    build_index_accepts_timeout = True

    def build_index(self, repo_root, files, jobs=1, parse_timeout_micros=0):
        return _Index(f"timeout={parse_timeout_micros}")


class UntimedUpper(Upper):
    def build_index(self, repo_root, files, jobs=1):
        return _Index("no timeout")


def test_the_parse_timeout_reaches_only_indexes_that_take_it(tmp_path) -> None:
    registry.clear()
    registry.register(TimedLower())
    registry.register(UntimedUpper())
    repo = _repo(tmp_path, {"a.lo": "", "b.up": ""})
    entries = [ScanEntry("a.lo", "lower"), ScanEntry("b.up", "upper")]
    indexes = pipeline._build_indexes(repo, entries, parse_timeout_micros=1234)
    assert (indexes["lower"].name, indexes["upper"].name) == ("timeout=1234", "no timeout")
