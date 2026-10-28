# Vue (vue-router) — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_vue/` (`routes.py` route configs, `template.py`
`<router-link>`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests:
`tests/unit/test_roles_vue.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `{ path, component: X }` in a routes array (`createRouter` / `new VueRouter`) | Binding | `route`, `routeKind: page`, `endpoint` = full path, `handler` = `X` | One component reached by URL |
| `{ path, component: () => import('./X.vue') }` (the specifier relative, or through a `tsconfig` / Vite alias) | Binding | as above, `handler` = the component name, never the import path | One component, loaded lazily; the file goes in `handlerPath` |
| `{ path, components: { default: X, … } }` (named views) | Binding | `handler` = the `default` component | The default view is the page |
| `{ path, children }` (no component) | Wiring | statement stays plain; `path` prefixes the children | No handler of its own |
| `{ path, component, children }` with a child `path: ''` | Wiring | statement stays plain; prefix for the children | A layout: a child always fills its `<router-view>` |
| `{ path, component, children }` with no empty-path child | Binding (and wiring for the children) | `route`, `page`, `handler` = component | Its own URL shows it with an empty outlet |
| `{ path, redirect }` (no component) | None | — | An alias; no handler |
| `beforeEnter` on a route | Guard | names in `guards` of that route and of its child routes (after `beforeEach`, parent first) | Runs before its page; entering a child enters its parents first |
| `router.beforeEach(...)` | Guard | — | Runs before every page |
| `fetch` / `axios` calls | Outbound | `api_call` | |
| `<router-link to>` / `<RouterLink :to>` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path | A link to a page |
| `router.push(...)` / `this.$router.push(...)` / `replace` | Outbound | `route`, `routeKind: navigation`, `endpoint` = target path. `router` counts when bound from `useRouter()` imported from `'vue-router'` (Next.js has a `useRouter` of its own) | Programmatic navigation |
| `createApp(App).mount(...)` / `new Vue({...})` | None | — | Start-up |
