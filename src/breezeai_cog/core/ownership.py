"""The ownership step: the language of a file whose extension several languages register.

An extension is normally registered by one base language, and the scan labels the file by
it. ``.h`` is registered by both C and C++, and the file alone rarely settles which: most
headers are valid C *and* valid C++. What settles it is the rest of the repo, chiefly which
files include the header. So these **shared** files are decided here, once, after the scan
and before any index is built, from evidence each candidate language supplies through its
``Ownership`` (``parsers/base.py``).

Each shared file goes through the rules in order; the first rule with one answer wins:

1. **content** — syntax only one candidate allows (C++ ``namespace``);
2. **references** — the languages of the files that reference it, followed back through other
   shared files. One language decides it; several decide it for the candidate every one of
   them can reference (a header included from C and C++ is C);
3. **neighbours**, then **repo** — the languages of the unshared candidate-language files in
   its folder, then in the whole repo;
4. **default** — the candidate that declares the extension its default, else the first
   registered (what the scan chose before the step existed).

Evidence is read as bytes, never parsed. A reference that matches no scanned file, or more
than one, is not evidence (honest-null). The rules depend only on the evidence, never on the
order files are visited, so every run on the same repo decides the same way.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from ..logging import get_logger
from ..parsers.base import Ownership
from ..parsers.index_common import parallel_map
from .registry import language_candidates, registered
from .scanner import ScanEntry

log = get_logger("breezeai_cog.ownership")


@dataclass(frozen=True, slots=True)
class Decision:
    """A shared file's language, and the rule that decided it (for the debug log)."""

    language: str
    rule: str  # content | references | neighbours | repo | default


def shared_languages(path: str) -> tuple[str, ...]:
    """The candidate languages of a shared file, in registration order; ``()`` when a single
    language registers the file's extension."""
    candidates = language_candidates(path)
    return tuple(p.name for p in candidates) if len(candidates) > 1 else ()


def competing_languages(languages: set[str]) -> set[str]:
    """Every language that shares an extension with one of ``languages``. A run limited to
    ``--language c`` still needs the C++ files as evidence for the shared headers."""
    out: set[str] = set()
    for parser in registered():
        if parser.name not in languages:
            continue
        for ext in parser.extensions:
            out.update(shared_languages(f"_{ext}" if ext.startswith(".") else ext))
    return out - languages


#: One file's evidence: the candidate languages its content proves, and its references.
_Evidence = tuple[tuple[str, ...], tuple[str, ...]]


def _evidence(
    args: tuple[
        str,
        tuple[tuple[str, Callable[[bytes], bool]], ...],
        tuple[Callable[[bytes], list[str]], ...],
    ],
) -> _Evidence:
    """Read one file and apply the hooks — pure, picklable worker for :func:`parallel_map`.
    A file that can't be read, or a hook that fails, gives no evidence (never a guess)."""
    abs_path, provers, referencers = args
    try:
        source = Path(abs_path).read_bytes()
    except OSError:
        return (), ()
    try:
        proved = tuple(lang for lang, proves in provers if proves(source))
        refs = tuple(dict.fromkeys(ref for find in referencers for ref in find(source)))
    except Exception as exc:  # a hook bug costs this file's evidence, not the run
        get_logger("breezeai_cog.ownership").warning(
            "ownership.evidence.failed",
            path=abs_path,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return (), ()
    return proved, refs


class _Files:
    """The scanned evidence files, for resolving a reference as written to one of them."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = frozenset(paths)
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for path in sorted(self.paths):
            self.by_name[posixpath.basename(path)].append(path)

    def resolve(self, referrer: str, ref: str) -> str | None:
        """The file ``ref`` names: relative to the referrer's folder, else the one file whose
        path ends with it. ``None`` when nothing or more than one file matches."""
        local = posixpath.normpath(posixpath.join(posixpath.dirname(referrer), ref))
        if local in self.paths:
            return local
        tail = posixpath.normpath(ref)
        while tail.startswith("../"):
            tail = tail[3:]
        matches = [
            p
            for p in self.by_name.get(posixpath.basename(tail), ())
            if p == tail or p.endswith("/" + tail)
        ]
        return matches[0] if len(matches) == 1 else None


def resolve_owners(
    repo_root: Path, entries: Sequence[ScanEntry], jobs: int = 1
) -> dict[str, Decision]:
    """Decide the language of every shared file in ``entries`` (path → decision). Unshared
    entries keep the language the scan gave them and are only read as evidence."""
    shared = {e.path: langs for e in entries if (langs := shared_languages(e.path))}
    if not shared:
        return {}
    involved = {lang for langs in shared.values() for lang in langs}
    hints: dict[str, Ownership] = {
        p.name: own
        for p in registered()
        if p.name in involved and (own := getattr(p, "ownership", None)) is not None
    }
    # Unshared files of a candidate language: their language is known, and they are evidence.
    known = {e.path: e.language for e in entries if e.path not in shared and e.language in involved}

    paths = sorted(shared) + sorted(known)
    args = []
    for path in paths:
        langs = shared.get(path) or (known[path],)
        provers = (
            tuple(
                (lang, proves)
                for lang in langs
                if lang in hints and (proves := hints[lang].proves) is not None
            )
            if path in shared
            else ()
        )
        referencers = tuple(
            dict.fromkeys(
                find
                for lang in langs
                if lang in hints and (find := hints[lang].references) is not None
            )
        )
        args.append((str(Path(repo_root) / path), provers, referencers))
    evidence = dict(zip(paths, parallel_map(args, _evidence, jobs)))

    files = _Files(paths)
    referrers: dict[str, set[str]] = defaultdict(set)
    for path, (_, refs) in evidence.items():
        for ref in refs:
            target = files.resolve(path, ref)
            if target is not None and target != path:
                referrers[target].add(path)

    decisions: dict[str, Decision] = {}
    for path, langs in shared.items():  # rule 1 first: rule 2 walks through these decisions
        proved = [lang for lang in evidence[path][0] if lang in langs]
        if len(proved) == 1:
            decisions[path] = Decision(proved[0], "content")
    by_content = dict(decisions)

    def includes(lang: str) -> tuple[str, ...]:
        return hints[lang].includes if lang in hints else ()

    folder_langs: dict[str, set[str]] = defaultdict(set)
    for known_path, lang in known.items():
        folder_langs[posixpath.dirname(known_path)].add(lang)
    repo_langs = set(known.values())

    for path, langs in shared.items():
        if path in decisions:
            continue
        decision = (
            _by_references(langs, _referrer_languages(path, referrers, known, by_content), includes)
            or _by_neighbours(path, langs, folder_langs, repo_langs)
            or _by_default(path, langs, hints)
        )
        decisions[path] = decision
    return decisions


def _referrer_languages(
    path: str,
    referrers: dict[str, set[str]],
    known: dict[str, str],
    by_content: dict[str, Decision],
) -> set[str]:
    """The languages of the files that reference ``path``, directly or through shared files
    no rule has decided yet (``main.cpp → api.h → list.h`` counts ``main.cpp`` for ``list.h``)."""
    langs: set[str] = set()
    seen = {path}
    queue = deque([path])
    while queue:
        for referrer in referrers.get(queue.popleft(), ()):
            if referrer in seen:
                continue
            seen.add(referrer)
            if referrer in known:
                langs.add(known[referrer])
            elif referrer in by_content:
                langs.add(by_content[referrer].language)
            else:  # an undecided shared file — walk on to whoever references it
                queue.append(referrer)
    return langs


def _by_references(
    langs: tuple[str, ...], referring: set[str], includes: Callable[[str], tuple[str, ...]]
) -> Decision | None:
    """Rule 2: one referring language that is a candidate decides it; several decide it for the
    one candidate every referring language can reference."""
    if not referring:
        return None
    if len(referring) == 1 and (only := next(iter(referring))) in langs:
        return Decision(only, "references")
    fits = [c for c in langs if all(c == r or c in includes(r) for r in referring)]
    return Decision(fits[0], "references") if len(fits) == 1 else None


def _by_neighbours(
    path: str, langs: tuple[str, ...], folder_langs: dict[str, set[str]], repo_langs: set[str]
) -> Decision | None:
    """Rule 3: the one candidate language among the unshared files in the same folder, else
    in the whole repo."""
    near = folder_langs.get(posixpath.dirname(path), set()) & set(langs)
    if len(near) == 1:
        return Decision(near.pop(), "neighbours")
    anywhere = repo_langs & set(langs)
    if len(anywhere) == 1:
        return Decision(anywhere.pop(), "repo")
    return None


def _by_default(path: str, langs: tuple[str, ...], hints: dict[str, Ownership]) -> Decision:
    """Rule 4: the candidate that declares the extension its default, else the first
    registered."""
    ext = posixpath.splitext(path)[1].lower()
    defaults = [lang for lang in langs if lang in hints and ext in hints[lang].default_for]
    return Decision(defaults[0] if len(defaults) == 1 else langs[0], "default")
