"""Next.js file conventions shared by the Next.js route detector (``typescript_nextjs``) and the
React parser (``typescript_react``), which keeps a Next.js route handler file that imports React a
Next.js file. One framework's package never imports another's (architecture §15)."""

from __future__ import annotations

import re

_ROUTE_STEMS = {"route"}  # App Router API handler file; page/layout are UI, not verb handlers


def is_app_router_route_file(path: str) -> bool:
    """A file that can hold App Router verb handlers: ``…/app/**/route.<ext>`` (the ext may be a
    custom page extension, ``route.api.ts``; the app's ``pageExtensions`` decide later)."""
    segs = path.replace("\\", "/").split("/")
    stem = segs[-1].split(".", 1)[0]
    return stem in _ROUTE_STEMS and "app" in segs[:-1]


def is_pages_api_file(path: str) -> bool:
    """A file that can hold a Pages Router API handler: ``…/pages/api/**`` (any stem)."""
    segs = path.replace("\\", "/").split("/")[:-1]  # dirs only
    return "pages" in segs and segs[segs.index("pages") + 1 : segs.index("pages") + 2] == ["api"]


# A verb handler in either accepted form: ``export … function GET`` / ``export const GET =``.
_VERB_EXPORT_SIG = re.compile(
    rb"export\s+(?:async\s+)?(?:function\s+|(?:const|let|var)\s+)"
    rb"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
)
# A Pages Router handler is the file's default export (any shape).
_DEFAULT_EXPORT_SIG = re.compile(rb"export\s+default\b")


def is_route_file(path: str, source: bytes) -> bool:
    """A file that may hold route handlers: an App Router ``route.*`` exporting a verb, or a
    ``pages/api/**`` file with a default export (path first, then a cheap byte sniff)."""
    if is_app_router_route_file(path):
        return bool(_VERB_EXPORT_SIG.search(source))
    if is_pages_api_file(path):
        return bool(_DEFAULT_EXPORT_SIG.search(source))
    return False
