# Next.js — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_nextjs/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_nextjs.py`

Next.js routes by file location, so the address comes from the path. Route handlers
(`nextjs-routes`) and pages (`nextjs-pages`) are additive detectors: a route file another parser
owns — Apollo's `app/api/graphql/route.ts`, a route importing a `@nestjs/…` client — keeps its
routes.

| Construct | Role | Record | Why |
|---|---|---|---|
| `export function GET(req)` / `export const POST = …` in `app/**/route.ts` | Binding | `route`, the verb, `routeKind: route`, directory path as `endpoint`, `handler` = the export name | One handler per verb |
| `export default function handler(req, res)` in `pages/api/**` | Binding | `route`, `method: ANY`, `routeKind: route`, file path from `pages` as `endpoint`, `handler` = the name | One handler; it branches on `req.method` itself |
| `export default (req, res) => …` in `pages/api/**` | Binding | as above; `handler` empty, `handlerLine` = where the function starts | Anonymous handler |
| Route group `(name)` / parallel slot `@name` directory | Wiring | — (not part of the address) | Organises files only |
| `page.tsx` under a parallel slot `@name` | Wiring | — | Drawn inside the parent's layout, at the parent's URL (the parent's page is the record) |
| `basePath` in `next.config.js` | Wiring | — (prefix for every binding) | Applies to all handlers |
| `pageExtensions` in `next.config.js` | Wiring | — (which `pages/**` files and which `app/**` `page` / `route` files count; the extension is not in the address: `index.page.tsx` → `/`, `app/x/page.page.tsx` → `/x`) | Not a literal list → no records for that app's pages or routes (unknown which files they are) |
| Default export of `app/**/page.tsx` | Binding | `route`, `routeKind: page`, directory path as `endpoint`, `handler` = the component | A page reached by URL |
| Default export of `pages/**` outside `pages/api` | Binding | `route`, `routeKind: page`, file path as `endpoint`, `handler` = the component | A page reached by URL |
| `app/**/page.mdx` / `pages/**/*.mdx`, when `pageExtensions` lists `mdx` (as `@next/mdx` requires) | Binding | `route`, `routeKind: page`, the path as `endpoint`, no `handler`, `nodeType: synthetic` (MDX is not parsed; the record comes from the path) | A page reached by URL; its content is the page |
| `app/**/layout.tsx` | Wiring | — | Layout around many pages |
| `middleware.ts` / `proxy.ts` (Next.js 16+; ignored when `package.json` pins `next` below 16) | Guard | `middleware` / `proxy` in `guards` of the paths its `matcher` covers; `authRequired` stays empty | Runs before many handlers; may only rewrite or log |
| `fetch(...)` | Outbound | `api_call` | Our code calling out |

Every record reports the node it stands for: `nodeType` is `export_statement` (a page's default
export, a route's exported verb or default handler) or the handler's declaration
(`export default handler`), and `text` is that node's source.
