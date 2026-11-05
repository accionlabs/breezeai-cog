"""Additive detector: ASP.NET routes in files with no ASP.NET ``using`` line.

A .NET 6+ web project turns on implicit usings, so its files need no ``using`` line at all::

    <Project Sdk="Microsoft.NET.Sdk.Web"> … <ImplicitUsings>enable</ImplicitUsings> …   // Api/Api.csproj
    var app = WebApplication.CreateBuilder(args).Build();                              // Api/Program.cs
    app.MapGet("/direct", () => "d");                                                  //   → GET /direct

:class:`~.parser.AspNetCoreParser` claims a file by the namespace it names, so such a file stays
with the plain C# parser. Parser selection is not widened (it would take files from the WCF and
Web Forms parsers, which share its priority); instead the module registers a fact beside a
detector (see :mod:`..additive`):

* **fact** ``aspnet.web-projects`` — while the C# index is built, ``_collect`` reads every
  ``.csproj`` (``FactSpec.sources``) and ``_resolve`` keeps each project directory with whether
  it is a web project: the Web SDK, or a ``Microsoft.AspNetCore.App`` framework reference (a
  class library holding endpoint extension methods);
* **detector** — while a C# file is parsed (by whichever parser owns it), ``_detect`` runs the
  ASP.NET route detection when the file's nearest project is a web project, or the file names an
  ASP.NET-only type (``WebApplication``, ``RouteGroupBuilder``, …) — the case with no ``.csproj``
  in the repo.

A file the ASP.NET parser owns is left to the parser; the two never both emit for one file.
Routes still come only from real ``Map*`` calls, route attributes and controllers.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..additive import (
    DetectContext, Detection, Detector, FactSpec, Gate, index_fact, register_detector,
    register_fact,
)
from .parser import detect_aspnet_routes, owned_by_aspnet

#: The detector's name.
NAME = "aspnet-web-projects"

#: The name its fact is stored under on the C# index.
FACT = "aspnet.web-projects"

#: Types only ASP.NET declares: a file naming one is ASP.NET code whatever its project says.
_TYPE_MARKERS = (
    b"WebApplication", b"IEndpointRouteBuilder", b"RouteGroupBuilder", b"ControllerBase",
    b"ApiController",
)

_XML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: ``<Project Sdk="Microsoft.NET.Sdk.Web">`` (optionally ``/version``) or ``<Sdk Name="…" />``.
_WEB_SDK = re.compile(r"""\b(?:Sdk|Name)\s*=\s*["']Microsoft\.NET\.Sdk\.Web(?:/[^"']*)?["']""")
#: ``<FrameworkReference Include="Microsoft.AspNetCore.App" />``.
_ASPNET_FRAMEWORK = re.compile(
    r"""<FrameworkReference\b[^>]*\bInclude\s*=\s*["']Microsoft\.AspNetCore\.App["']""")


@dataclass(frozen=True)
class WebProjects:
    """``projects``: every project directory (``""`` = the repo root) → whether it is a web
    project, longest first so the first match is a file's nearest project."""

    projects: tuple[tuple[str, bool], ...] = field(default_factory=tuple)

    def is_web(self, path: str) -> bool:
        """Whether the nearest project above ``path`` is a web project."""
        for proj, web in self.projects:
            if proj == "" or path.startswith(proj + "/"):
                return web
        return False


def web_projects(index: Any | None) -> WebProjects:
    """The resolved fact, or an empty one (no index, e.g. single-file parsing)."""
    fact = index_fact(index, FACT)
    return fact if isinstance(fact, WebProjects) else WebProjects()


def _collect(root: None, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, bool]:
    """``(project dir, is web)`` for one ``.csproj``. Comments are dropped first, so a
    commented-out SDK or reference does not count."""
    text = _XML_COMMENT.sub("", source.decode("utf-8", "replace"))
    web = bool(_WEB_SDK.search(text) or _ASPNET_FRAMEWORK.search(text))
    return posixpath.dirname(rel), web


def _resolve(collected: list[tuple[str, bool]], index: object) -> WebProjects | None:
    """Every project directory, nearest first. Two projects in one directory make it a web
    project when either is one (their files are not told apart)."""
    dirs: dict[str, bool] = {}
    for proj, web in collected:
        dirs[proj] = dirs.get(proj, False) or web
    if not any(dirs.values()):
        return None
    return WebProjects(tuple(sorted(dirs.items(), key=lambda kv: (-len(kv[0]), kv[0]))))


def _detect(dc: DetectContext) -> Detection | None:
    """ASP.NET routes of a file the ASP.NET parser does not own, when its project or one of its
    types says it is ASP.NET code."""
    if owned_by_aspnet(dc.source):
        return None
    if not (any(m in dc.source for m in _TYPE_MARKERS)
            or web_projects(dc.ctx.resolution_index).is_web(dc.path)):
        return None
    routes, wiring = detect_aspnet_routes(dc.record, dc.root, dc.ctx)
    return Detection(routes, label="aspnet") if routes or wiring else None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"<Project",), sources=(".csproj",),
    collect=_collect, resolve=_resolve,
))

register_detector(Detector(
    name=NAME,
    # No byte guard: the marker may be in the .csproj; _detect's checks are byte and prefix checks.
    gate=Gate("csharp", order=35, skip_fixtures=True),
    detect=_detect, frameworks=("aspnet",),
))
