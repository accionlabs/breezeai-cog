# Fastify — entry-point roles

Parser: `src/breezeai_cog/parsers/typescript_fastify/` (additive detector, every TS / JS file) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_fastify.py`

**What is an instance.** Routes are registered on a Fastify instance, and cog decides what is one
from how the name is bound, never from the name alone (`app`, `server` and `fastify` are common
names for other things too):

- an app: a variable set to the `fastify` package's factory (`const app = Fastify()`,
  `require('fastify')(…)`);
- a plugin: the first parameter of a function passed to `register` in the same file, of an
  exported function another file registers or `@fastify/autoload` loads, or of a function typed
  `FastifyInstance` / `FastifyPlugin*`.

An exported function with none of these (no Fastify type, no registration found) is not a plugin,
even in a file that imports `fastify`.

| Construct | Role | Record | Why |
|---|---|---|---|
| `fastify.METHOD(path, [options,] handler)` (`get` `post` `put` `delete` `patch` `head` `options`) with a named handler | Binding | `route`, the verb, `routeKind: route`, `framework: fastify`, `endpoint` = the instance's prefix + `path`, `handler` = the name, `handlerLine` = its declaration in this file | One handler, inbound API |
| Same, with an inline function, or the handler in the options (`{ handler }`) | Binding | as above; an inline function has no `handler`, and `handlerLine` = where it starts | Anonymous handler |
| `fastify.all(path, handler)` | Binding | as above, `method: ANY` | Every verb |
| `fastify.route({ method, url \| path, handler })` | Binding per verb | one `route` per verb of `method` (a string or an array) | Each verb is served by the handler |
| A route with `websocket: true` in its options (`@fastify/websocket`) | Binding | `route`, `method: WEBSOCKET`, `routeKind: ws` | A connection handler |
| `onRequest` / `preParsing` / `preValidation` / `preHandler` in a route's options | Guard | their named functions in `guards` (`fastify.auth` for `fastify.auth([…])`); no `authRequired` — a hook may validate, log or parse, and `fastify.auth` only combines the app's own checks | Run before this handler |
| `fastify.addHook('onRequest' \| 'preParsing' \| 'preValidation' \| 'preHandler', fn)` | Guard | `fn` in `guards` of the instance's routes registered after it, and of the plugins it registers after it; no `authRequired` | Runs before many handlers |
| `fastify.register(plugin, { prefix })` | Wiring | statement stays plain; `prefix` is joined in front of the plugin's routes — an inline function, a function of this file, or another file's plugin, imported by a relative path or a `tsconfig` alias (`fastify.plugin-prefixes`) | Points at a plugin, no handler |
| `setupRoutes(app)` — a function of the repo called with an instance as its first argument | Wiring | as `register` without a prefix: the function's routes are served at the instance's prefix; called with two instances under different prefixes → empty `endpoint` | It runs on that instance |
| `fastify.register(fp(plugin), …)` / a plugin exported as `fp(…)` (`fastify-plugin`) | Wiring | as above, but a `prefix` is ignored: the plugin's routes are served at its parent's prefix, as Fastify does | `fastify-plugin` skips encapsulation |
| `fastify.register(AutoLoad, { dir, options })` (`@fastify/autoload`) | Wiring | statement stays plain; every plugin file under `dir` gets the prefix autoload gives it: `options.prefix`, then its folder path (`routes/users/index.js` → `/users`; none with `dirNameRoutePrefix: false`), or an exported `autoPrefix` / `prefixOverride` / `autoConfig.prefix`. In a folder with an `index` file only that file is loaded. Named hooks of an `autohooks` file (`autoHooks: true`) guard its folder | One registration, a prefix per file |
| A plugin registered under two different prefixes, or a prefix that can't be read (`{ prefix: config.base }`, an autoload option given as a function) | Wiring | its routes have an empty `endpoint` | Never a partial or guessed address |
| A file in an autoload folder that autoload doesn't load (beside an `index` file, filtered out) and nothing registers | Binding | its routes have an empty `endpoint` | How it is served is unknown |
| A typed plugin nothing in the repo registers | Binding | `endpoint` = the route's own path; empty when some autoload folder can't be resolved (it may be loaded from there) | Served at the app's root unless a registration says otherwise |
| Route path with an unresolved part (`fastify.get(settings.path, h)`) | Binding | `endpoint` empty | Never a partial or guessed address |
| `fastify.register(cors)` / any package's plugin | None | — | Not our routes |
| `setErrorHandler` / `setNotFoundHandler` / `decorate` / `listen` | None | — | Not an address of their own |
| `client.request({ method, path })` / `fetch(...)` | Outbound | `api_call` | Our code calling out; an object with a `handler` is a route declaration, never an outbound call |
