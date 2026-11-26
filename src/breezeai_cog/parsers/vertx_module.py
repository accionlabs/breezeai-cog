"""A Vert.x 2 module descriptor (``mod.json``) and the verticle its ``"main"`` names.

Deploying the module (``vertx runmod …``, ``container.deployModule(…)``) makes Vert.x create
that verticle and call its ``start()``. The descriptor is the only place this wiring is
written, so without it the verticle's ``start()`` has no caller. :func:`module_deploy` records
it as one ``verticle_deploy`` statement on the ``mod.json`` File (start-up wiring, entry role
None, like ``deployVerticle``):

======================================  ===================================================
``"main"``                              Statement
======================================  ===================================================
a repo class declaring ``start()``      ``endpoint`` the class; ``handler`` ``start`` with
                                        its ``handlerPath`` / ``handlerLine``
a repo class without its own ``start``  ``endpoint`` only (the inherited one is not located)
a class not in the repository           ``endpoint`` only
a script verticle (``app.js``)          ``endpoint`` the script; ``handlerPath`` when the
                                        file sits next to ``mod.json``
no ``"main"`` (``"includes"`` only)     none
no sign of Vert.x (below)               none
======================================  ===================================================

A file named ``mod.json`` with a ``"main"`` is not always a Vert.x descriptor. It is one only
with a Vert.x verticle factory prefix, a key only a module descriptor has (``worker``,
``multi-threaded``, ``includes``, ``preserve-cwd``, ``auto-redeploy``, ``resident``,
``system``), or a ``"main"`` naming a Java or Groovy class of the repository; otherwise it
gives no statement (a wrong deploy is worse than a missing one).

A leading language prefix (``groovy:com.acme.App``) selects the verticle factory, not the
class: it is stripped from ``endpoint``, as from the File's ``verticleMain``. A ``groovy:``
class is looked up among the repository's Groovy classes first, then its Java classes: the
Groovy factory loads a compiled class by name, so a Java verticle deployed through it runs as
written. A class is found
through the language index by its fully-qualified name, and ``start`` only in the file that
name maps to (``handler_sites.locate_method`` with ``within``); a name declared twice, or two
``start`` declarations there (``start()`` and ``start(Future)``), leave it unlocated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ..emit import file_id, statement_id
from ..schemas import Statement
from .handler_sites import handler_location, locate_method

#: Vert.x 2 verticle factory prefixes (``langs.properties``): ``groovy:App`` runs ``App`` with
#: the Groovy factory.
_LANGUAGE_PREFIX = re.compile(r"^([a-z]+):(?=\S)")
#: Extensions of a verticle given as a source file, which Vert.x 2 compiles or interprets.
_SCRIPT_EXTENSIONS = (".js", ".groovy", ".rb", ".py", ".java", ".scala", ".clj", ".coffee")
#: The ``"main"`` key, for the statement's line.
_MAIN_KEY = re.compile(r'"main"\s*:')
#: The method Vert.x calls on the verticle it creates.
_START = "start"
#: Verticle factory prefixes of Vert.x 2's ``langs.properties`` (and the usual extra ones).
_VERTX_LANGUAGES = frozenset({
    "java", "groovy", "rhino", "nashorn", "nodyn", "js", "jruby", "ruby", "jython", "python",
    "scala", "clojure",
})
#: Keys only a Vert.x 2 module descriptor has (``main`` aside, which other tools use too).
_MODULE_KEYS = frozenset({
    "worker", "multi-threaded", "includes", "preserve-cwd", "auto-redeploy", "resident", "system",
})


def module_deploy(path: str, text: str, data: Any, repo_root: Path,
                  indexes: Mapping[str, Any]) -> Statement | None:
    """The ``verticle_deploy`` statement of a ``mod.json`` whose ``"main"`` names a verticle,
    else None. ``indexes`` holds the ``java`` / ``groovy`` indexes (absent or None when the
    repository has no such files or the build failed)."""
    main = data.get("main") if isinstance(data, dict) else None
    if not isinstance(main, str) or not main.strip():
        return None
    main = main.strip()
    prefix = _LANGUAGE_PREFIX.match(main)
    name = main[prefix.end():] if prefix else main
    if not _is_vertx_module(data, prefix.group(1) if prefix else None, name, indexes):
        return None  # a `mod.json` of another tool: no verticle is deployed
    handler: str | None = None
    line: int | None = None
    handler_path: str | None = None
    if name.endswith(_SCRIPT_EXTENSIONS):
        script = (PurePosixPath(path).parent / name).as_posix()
        if (repo_root / script).is_file():
            handler_path = script
    else:
        groovy = prefix is not None and prefix.group(1) == "groovy"
        languages = ("groovy", "java") if groovy else ("java",) if prefix else ("java", "groovy")
        site = _start_site(name, languages, indexes, first=groovy)
        if site is not None:
            handler = _START
            line, handler_path = handler_location(site, path)
    stmt_line, column, written = _main_line(text)
    return Statement(
        id=statement_id(path, stmt_line, column),
        parentId=file_id(path),
        nodeType="synthetic",
        text=written,
        startLine=stmt_line,
        endLine=stmt_line,
        path=path,
        semanticType="verticle_deploy",
        framework="vertx",
        endpoint=name,
        handler=handler,
        handlerLine=line,
        handlerPath=handler_path,
    )


def _is_vertx_module(data: dict[str, Any], language: str | None, name: str,
                    indexes: Mapping[str, Any]) -> bool:
    """Whether a ``mod.json`` with a ``"main"`` is a Vert.x 2 module descriptor. The file name and
    a ``"main"`` key alone are not enough (other tools write them); any one of these is: a Vert.x
    verticle factory prefix (``groovy:``), a key only a module descriptor has (``worker``,
    ``includes`` …), or a ``"main"`` naming a Java or Groovy class of the repository."""
    if language is not None and language in _VERTX_LANGUAGES:
        return True
    if _MODULE_KEYS & data.keys():
        return True
    return any(getattr(indexes.get(lang), "fqcn", {}).get(name) for lang in ("java", "groovy"))


def _start_site(fqcn: str, languages: tuple[str, ...], indexes: Mapping[str, Any], *,
                first: bool = False) -> tuple[str, int] | None:
    """``(file, line)`` of ``start`` declared by the class ``fqcn``, looked up in each of
    ``languages``; None unless exactly one language declares the class and its ``start``.
    With ``first``, the first of ``languages`` that declares the class decides alone."""
    found: list[tuple[str, int]] = []
    for language in languages:
        index = indexes.get(language)
        file = getattr(index, "fqcn", {}).get(fqcn) if index is not None else None
        if not file:  # not declared in this language, or declared twice (None)
            continue
        site = locate_method(index, language, fqcn, _START, within={file})
        if site is None:
            return None  # the class is here but its own `start` is not (inherited, overloaded)
        if first:
            return site
        found.append(site)
    return found[0] if len(found) == 1 else None


def _main_line(text: str) -> tuple[int, int, str]:
    """(line, column, line text) of the ``"main"`` key — the line that names the verticle;
    the first line when the key is not found as written (a JSONC quirk)."""
    hit = _MAIN_KEY.search(text)
    offset = hit.start() if hit else 0
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    written = text[line_start:line_end if line_end != -1 else len(text)].strip()
    return text.count("\n", 0, offset) + 1, offset - line_start, written
