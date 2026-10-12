# Next.js — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_nextjs/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_nextjs.py`

Next.js routes by file location, so the address comes from the path.

| Construct | Role | Record | Why |
|---|---|---|---|
| `export function GET(req)` / `export const POST = …` in `app/**/route.ts` | Binding | `route`, the verb, `routeKind: route`, directory path as `endpoint`, `handler` = the export name | One handler per verb |
| `export default function handler(req, res)` in `pages/api/**` | Binding | `route`, `method: ANY`, `routeKind: route`, file path from `pages` as `endpoint`, `handler` = the name | One handler; it branches on `req.method` itself |
| `export default (req, res) => …` in `pages/api/**` | Binding | as above; `handler` empty, `handlerLine` = where the function starts | Anonymous handler |
| Route group `(name)` / parallel slot `@name` directory | Wiring | — (not part of the address) | Organises files only |
| `basePath` in `next.config.js` | Wiring | — (prefix for every binding) | Applies to all handlers |
| Default export of `app/**/page.tsx` | Binding | `route`, `routeKind: page`, directory path as `endpoint`, `handler` = the component | A page reached by URL |
| Default export of `pages/**` outside `pages/api` | Binding | `route`, `routeKind: page`, file path as `endpoint`, `handler` = the component | A page reached by URL |
| `app/**/layout.tsx` | Wiring | — | Layout around many pages |
| `middleware.ts` | Guard | its name in `guards` of the paths its `matcher` covers | Runs before many handlers |
| `fetch(...)` | Outbound | `api_call` | Our code calling out |
