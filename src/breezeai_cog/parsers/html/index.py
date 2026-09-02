"""Repo-level pre-pass: resolve Angular component templates to their owning component.

An Angular component and its template live in **two files** — the ``.ts`` class declares
``templateUrl: './x.component.html'``, the markup lives in the ``.html``. The template file
is parsed on its own (one file → one parser), so it cannot see the decorator that owns it.
This index closes that gap: it scans every component ``.ts`` for ``templateUrl`` and maps the
resolved ``.html`` path back to its component (path + class + selector), so the template
parser can stamp ``framework``/``importFiles`` and the child-component selector index (P2).

Resolution is **honest**: a template is mapped only when the ``templateUrl`` resolves to a
file that actually exists in the repo, and a template claimed by two different components is
dropped as ambiguous (no link) rather than attributed to one — an absent edge beats a wrong
one. The ``.ts`` scan is a bounded regex over the ``templateUrl`` / ``selector`` string
contract (not a guess about behaviour), so it needs no TypeScript parse here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..index_common import parallel_map

# Directory names never worth walking for component sources (vendored / build output / VCS).
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "bower_components",
        "dist",
        "out",
        "build",
        ".git",
        ".angular",
        ".nx",
        "coverage",
        ".cache",
    }
)

# The ``templateUrl: '…'`` / ``selector: '…'`` string contract (single, double, or backtick
# quotes). Value is the path/selector literal; injected/computed values simply don't match
# (honest-null — no link).
_TEMPLATE_URL_RE = re.compile(r"""templateUrl\s*:\s*['"`]([^'"`]+)['"`]""")
_SELECTOR_RE = re.compile(r"""selector\s*:\s*['"`]([^'"`]+)['"`]""")
# The component class immediately following its decorator; scanned forward from a templateUrl
# hit within a bounded window.
_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")
#: Chars scanned around a ``templateUrl`` hit to find its component class / selector — a
#: whole ``@Component({…}) export class X`` declaration sits comfortably within this window.
_DECL_WINDOW = 2000


@dataclass(frozen=True)
class ComponentRef:
    """The component that owns a template — the endpoints of the html→component link, plus the
    framework that resolved it (so the parser stamps a real framework and picks the right
    grammar instead of assuming one — ``.html`` is not single-framework)."""

    path: str  # repo-relative component source path (drives importFiles → IMPORTS)
    className: str
    framework: str = "angular"  # the framework whose resolver claimed this template
    selector: str | None = None  # component selector (for the P2 child-component index)


@dataclass
class HtmlTemplateIndex:
    """template repo-relative path → owning :class:`ComponentRef`. Picklable (threaded into
    every ParseContext across the spawn pool)."""

    by_template: dict[str, ComponentRef] = field(default_factory=dict)

    def component_for(self, template_path: str) -> ComponentRef | None:
        return self.by_template.get(template_path.replace("\\", "/"))


def _ts_files(repo_root: Path) -> list[str]:
    """Every ``.ts`` file under ``repo_root``, pruning vendor/build/VCS directories."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".ts") and not name.endswith(".d.ts"):
                out.append(os.path.join(dirpath, name))
    return out


def _scan_ts_file(item: tuple[str, str]) -> list[tuple[str, str, str, str | None]]:
    """Worker: extract ``(template_repo_rel, component_repo_rel, className, selector)`` for each
    ``templateUrl`` in one ``.ts`` file. Module-level + plain-data args so it is picklable for
    ``parallel_map``. Only templates whose resolved path exists on disk are returned."""
    repo_root_str, ts_path = item
    repo_root = Path(repo_root_str)
    try:
        source = Path(ts_path).read_text("utf-8", "replace")
    except OSError:
        return []
    if "@Component" not in source or "templateUrl" not in source:
        return []  # cheap guard — skip non-component TS
    if "@angular/" not in source:
        # A `@Component({templateUrl})` is only an *Angular 2+* component when the file imports
        # from `@angular/` (matches AngularParser.claims). Without it — AngularJS views, a
        # look-alike decorator from another lib — we do NOT claim it as an Angular template
        # (honest-by-construction; it stays a standalone .html rather than a mislabelled one).
        return []

    ts_dir = Path(ts_path).parent
    try:
        component_rel = Path(ts_path).resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return []
    results: list[tuple[str, str, str, str | None]] = []
    for m in _TEMPLATE_URL_RE.finditer(source):
        rel = m.group(1)
        target = (ts_dir / rel).resolve()
        if not target.is_file():
            continue  # unresolved / injected path → no link
        try:
            template_rel = target.relative_to(repo_root).as_posix()
        except ValueError:
            continue  # outside the repo → not an in-repo edge
        # The component class is the next `class X` after the templateUrl (within the decorator
        # + declaration window); the selector is the nearest one in that same window.
        window = source[m.start() : m.start() + _DECL_WINDOW]
        cls_m = _CLASS_RE.search(window)
        if cls_m is None:
            continue  # no class found → cannot name the component (honest-null)
        sel_m = _SELECTOR_RE.search(window) or _SELECTOR_RE.search(
            source[max(0, m.start() - _DECL_WINDOW) : m.start()]
        )
        results.append(
            (template_rel, component_rel, cls_m.group(1), sel_m.group(1) if sel_m else None)
        )
    return results


def build_html_template_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1
) -> HtmlTemplateIndex:
    """Scan every component ``.ts`` under ``repo_root`` and build the template→component map.
    A template claimed by two different components is dropped as **ambiguous** (no link)."""
    ts_paths = _ts_files(repo_root)
    items = [(str(repo_root), p) for p in ts_paths]
    scanned = parallel_map(items, _scan_ts_file, jobs)

    by_template: dict[str, ComponentRef] = {}
    ambiguous: set[str] = set()
    for file_results in scanned:
        for template_rel, component_rel, class_name, selector in file_results:
            ref = ComponentRef(path=component_rel, className=class_name, selector=selector)
            existing = by_template.get(template_rel)
            if existing is None:
                by_template[template_rel] = ref
            elif existing.path != ref.path or existing.className != ref.className:
                ambiguous.add(template_rel)  # two distinct owners → drop (honest-null)
    for t in ambiguous:
        by_template.pop(t, None)
    return HtmlTemplateIndex(by_template=by_template)
