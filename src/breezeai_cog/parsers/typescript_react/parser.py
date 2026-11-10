"""ReactParser — a TypeScript framework parser. Selected over the base TypeScriptParser
(single parser per file) when ``claims`` finds a ``react`` / ``react-router`` import; reuses
``TypeScriptParser.extract`` on the shared tree, then marks components and hooks. React Router
routes are the additive ``react-routes`` detector (``routes.py``), so a router file another
parser owns keeps them."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from ..typescript_nextjs.routes import is_route_file
from .components import mark_react_components, mark_react_hooks
from .routes import REACT_GUARDS as _REACT_GUARDS


class ReactParser(TypeScriptParser):
    name = "typescript-react"
    priority = 10
    frameworks = ["react"]

    def claims(self, path: str, source: bytes) -> bool:
        return any(g in source for g in _REACT_GUARDS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited base extraction (one parse)
        # Class components (`extends React.Component`) and function components (PascalCase +
        # renders JSX) -> uiRole. Nodes are always captured, so this is not gated on statements.
        mark_react_components(root, ctx.source, record)
        # Custom hooks — a `useX` function that calls a React hook primitive (`useState`/…).
        mark_react_hooks(root, ctx.source, record)
        # framework axis: every file this parser claims IS a React file — stamp it
        # unconditionally so "list React files" is answerable, not only on route files. The
        # JS/TS distinction lives on the orthogonal `language` axis (.tsx -> typescript,
        # .jsx -> javascript), set by the base extractor. A Next.js route handler file that
        # imports React (an OG-image `route.tsx`) stays a Next.js file.
        if not (record.framework == "nextjs" and is_route_file(ctx.path, ctx.source)):
            record.framework = "react"
        # Routes: the additive ``react-routes`` detector, already run inside extract().
        return record
