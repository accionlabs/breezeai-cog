# Template capture

How `breezeai-cog` handles markup and view files — `.html`, `.vue`, `.cshtml`, `.aspx` and
friends — and why they are **skipped by default**.

> **TL;DR** — Template files are not captured unless you pass `--capture-templates`.
> Use the flag for **Razor Pages / Blazor** and **Vue**, where the markup holds routes and
> methods that exist nowhere else. You can leave it off for React and plain backends (no
> markup to begin with) and for **Web Forms** (the code-behind keeps the routes).

---

## Why they are off by default

A template is mostly markup: elements, attributes, bindings. A single Angular or Web Forms
page can produce hundreds of nodes that say very little about what the system actually does,
and a large repo can easily inflate its graph by tens of thousands of nodes on markup alone.

When you later ask the graph "where are orders priced?", those nodes are noise — they crowd
out the code that holds the answer. So the default is to skip them, and the flag is there for
the cases where the markup genuinely *is* the logic.

---

## What counts as a template

Eight extensions, owned by four parsers. Each parser declares only its **markup** half — the
code half is captured either way.

| Extension | Parser | Also owns (always captured) |
|---|---|---|
| `.html` `.htm` | `html` | — |
| `.cshtml` `.razor` | `razor` | — |
| `.aspx` `.ascx` `.master` | `csharp-webforms` | `.cs`, including `.aspx.cs` code-behind |
| `.vue` | `typescript-vue` | `.ts` `.tsx` `.js` `.jsx` … |

For the live list, ask the tool — it is generated from the parsers, never hand-maintained:

```bash
breezeai-cog capabilities | jq .templateExtensions
```

Two details worth knowing:

- **Only the final extension counts.** `Page.aspx.cs` ends in `.cs`, so the code-behind is
  always captured. Only the bare `Page.aspx` is gated.
- **Case is ignored.** `Site.Master` and `Default.ASPX` are recognised as templates.

### Markup that is *not* gated

Plenty of other files contain markup tags. The flag does not touch them — it only ever
applies to the eight extensions above.

**Always captured, flag or no flag:**

| Extension | Captured as | Why it is not gated |
|---|---|---|
| `.tsx` `.jsx` | `typescript` / `javascript` | JSX is code — the markup lives inside a code file |
| `.svc` | `svc`, `framework=wcf` | A `<%@ ServiceHost %>` directive declares a *service*, not a view |
| `.asmx` | `asmx`, `framework=asmx` | A `<%@ WebService %>` directive — same reasoning |
| `.xml`, `web.config`, `.csproj`, `.sln` | `config` | XML tags, but configuration rather than a view |

`.svc` and `.asmx` are the surprising pair: they carry ASP.NET `<%@ %>` directives exactly
like `.aspx` does, but they describe service endpoints, so they stay in the graph.

**Never captured, flag or no flag** — these have no parser at all, so they are reported as
`unsupported` in either mode:

`.jsp` · `.hbs` · `.mustache` · `.ejs` · `.erb` · `.twig` · `.blade.php` · `.vm` · `.ftl` ·
`.jinja` · `.pug` · `.svelte` · `.xhtml` · `.resx`

If a JSP or Handlebars template is missing from your graph, `--capture-templates` will not
bring it back — that format is simply not supported yet.

---

## Turning it on

```bash
# default — templates skipped
breezeai-cog repo-to-json-tree --repo . --capture-statements

# templates included
breezeai-cog repo-to-json-tree --repo . --capture-statements --capture-templates
```

Or through the environment, which is also how the HTTP service picks it up:

```bash
export BREEZEAI_COG_CAPTURE_TEMPLATES=true
```

---

## Where the gate sits

Templates are dropped **during the directory walk**, before any file is opened — so leaving
the gate on makes a run faster, not slower.

```
   for each file the walk finds
              │
              ▼
   ┌──────────────────────┐   ignored by .gitignore / .repoignore /
   │  ignore / include    │──▶ built-in defaults, and not re-included
   └──────────┬───────────┘   by .repoinclude                → reason: ignored
              │
              ▼
   ┌──────────────────────┐   extension is markup and
   │  template gate       │──▶ --capture-templates is off    → reason: template
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  extension allow-list│──▶ no parser claims it           → reason: unsupported
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │  max file size       │──▶ bigger than the limit         → reason: oversized
   └──────────┬───────────┘
              │
              ▼
        parsed into the graph
```

The order matters twice over. The gate runs **after** ignore/include, so a template inside
`node_modules/` is still reported as `ignored` — the `template` count only ever means "a real
template we chose to skip". And it runs **before** classification, so the decision never
depends on which parser would have claimed the file.

### Interaction with the ignore files

The template gate is a capture-scope decision, not an ignore rule. That makes the interaction
slightly surprising, so it is worth stating plainly:

| Situation | Result |
|---|---|
| `.repoignore` lists a template, flag **off** | Skipped as `ignored` |
| `.repoignore` lists a template, flag **on** | Skipped as `ignored` — `.repoignore` still wins |
| `.repoinclude` lists a template, flag **off** | Still skipped as `template` — `.repoinclude` does **not** re-include templates |
| `.repoinclude` lists a template, flag **on** | Captured |

In short: `.repoignore` can always take a template away; only `--capture-templates` can give
one back.

### Reading the skip report

Every run prints a breakdown and writes the full list to `.cog/<repo>-skipped-report.json`:

```
Skipped 3,716 file(s), 32 folder(s):
  unsupported (1,509) — .png 318, .svg 204, .scss 167, .rs 159 ...
  ignored     (1,313)
  template    (894)
```

`template` is its own bucket precisely so this is legible: if markup is missing from your
graph, the report tells you it was a deliberate choice, not a parser failure.

---

## The two-pass design for `.html`

`.html` is the awkward case. It is not one framework — it hosts Angular, AngularJS, Vue,
Aurelia, Knockout and Thymeleaf markup, plus plain static pages. And an Angular component
lives in **two files**: the `.ts` declares `templateUrl`, the markup sits in the `.html`.

Because every file is parsed by exactly one parser, in isolation, a template file cannot see
the decorator that owns it. So resolution happens in two passes:

```
  PASS 1 — repo-level pre-pass, before any template is parsed
  ═════════════════════════════════════════════════════════════
     scan every component .ts for the templateUrl / selector
     string contract
                        │
                        ▼
       @Component({                         resolve the path;
         selector: 'app-todo',      ──▶     keep it only if that
         templateUrl:                       file really exists
           './todo.component.html'
       })
       export class TodoComponent
                        │
                        ▼
            ┌────────────────────────────────────┐
            │ index:  todo.component.html        │
            │           → TodoComponent          │
            │           → framework: angular     │
            └────────────────────────────────────┘
            two components claiming one template
            → dropped as ambiguous (no link)

                        │  index threaded into every parse
                        ▼

  PASS 2 — per-file parse
  ═════════════════════════════════════════════════════════════
     a .html file
          │
          ├─ Tier 1: is it in the index?
          │          ✔ → framework from the component, plus an
          │              importFiles edge back to it   (authoritative)
          │
          ├─ Tier 2: does the markup carry uniquely-identifying
          │          syntax?  *ngFor → angular, ng-* → angularjs,
          │          v-* → vue, th: → thymeleaf, data-bind → knockout
          │          ✔ → framework named, but no owner link  (fallback)
          │
          └─ neither → a plain html File. No uiRole, no framework,
                       no statements.
```

The design is deliberately conservative at every step: a `templateUrl` is honoured only when
it resolves to a file that exists, a template claimed by two components is dropped rather
than attributed to one, and markup matching neither tier is never claimed as a template. An
absent link beats a wrong one.

Here is what pass 2 produces for a resolved Angular template, and for a static page that
matched neither tier:

```
src/todo.component.html   framework=angular  uiRole=template
                          importFiles = ['src/todo.component.ts']   ← the pass-1 link
                          statements:
                            structural_directive  *ngFor="let todo of todos"
                            event_binding         (click)="edit(todo.id)"   handler=edit
                            interpolation         {{ todo.title }}
                            property_binding      [routerLink]=...  route → /todo

src/orphan.html           framework=None  uiRole=None
                          (no statements)
```

> **Statements need both flags.** Markup is parsed into statements only when
> `--capture-statements` is also on *and* a grammar exists for that dialect — today, Angular.
> A Thymeleaf or Knockout page is still tagged with its framework but produces no statements:
> better an honest gap than statements parsed with the wrong grammar.

---

## Example — a Vue SFC

```vue
<!-- src/TodoItem.vue -->
<template>
  <li>
    <span @click="edit(todo)">{{ todo.title }}</span>
    <router-link :to="'/todo/' + todo.id">details</router-link>
  </li>
</template>

<script>
export default {
  methods: {
    edit(todo) { this.$emit('edit', todo.id) },
  },
}
</script>
```

With `--capture-templates`:

```
src/TodoItem.vue   framework=vue  uiRole=component
                   functions  = [edit]
                   statements:
                     directive_attribute   @click="edit(todo)"        handler=edit
                     interpolation         {{ todo.title }}
                     directive_attribute   :to="'/todo/' + todo.id"   route → /todo/
                     expression_statement  this.$emit('edit', todo.id)
```

This is the clearest argument for the flag on a Vue codebase: the `<script>` block is **real
code**, so `edit` is a genuine Function. `@click` records `handler=edit`, wiring the markup
back to the method beside it, and `<router-link>` becomes a `route` so navigation stays
discoverable. Skip `.vue` and all of that goes with it.

---

## What each template type produces

| File | `framework` | `uiRole` | Notable output |
|---|---|---|---|
| `.html` (resolved or fingerprinted) | `angular`, `angularjs`, `vue`, `aurelia`, `knockout`, `thymeleaf` | `template` | `interpolation`, `event_binding`, `property_binding`, `two_way_binding`, `structural_directive`, control-flow blocks |
| `.html` (plain page) | — | — | a File node only |
| `.cshtml` | `razor` | `template` | `@page` → a `route`; model and expression statements |
| `.razor` | `razor` | `component` | same, plus `@code { }` methods as **Functions** |
| `.aspx` `.ascx` `.master` | `aspnet-webforms` | `template` | `element` / `attribute`, with `handler=` for `On<Event>` wiring |
| `.vue` | `vue` | `component` | `directive_attribute`, `interpolation`, plus `<script>` Functions and Classes |

`uiRole` separates a **template** (a view rendered by something else) from a **component** (a
unit owning both markup and code). `.razor` and `.vue` are components; `.cshtml` and `.aspx`
are templates.

---

## Which stacks need the flag

| Stack | Safe to skip? | Why |
|---|---|---|
| React, plain backends (Spring, FastAPI, Express…) | ✅ Yes | No markup files to begin with |
| ASP.NET Web Forms | ✅ Mostly | Code-behind reads its sibling markup off disk, so routes, layouts and navigation survive |
| Angular (`.html` templates) | ⚠️ Partly | Components and routing survive; template bindings and `routerLink` navigation do not |
| Vue (`.vue` SFCs) | ❌ No | The `<script>` block is real code — methods, stores, composables |
| Razor Pages / Blazor | ❌ No | `@page` routes and `@code` methods exist **only** in the markup |

If you select a language that owns nothing but templates while the gate is on, the run would
produce nothing — so it warns rather than failing silently:

```
$ breezeai-cog repo-to-json-tree --repo . --language html
WARNING message=scan.templates_gated languages=["html"]
  hint="these languages only own template files, which are skipped by default;
        pass --capture-templates to analyze them"
```

---

## A note on links between files

Skipping a template does not leave broken references behind. A skipped template is still on
disk, so an import resolver could bind to it — TypeScript, for instance, resolves
`./TodoItem.vue`. Those references are pruned, so an `importFiles` entry never points at a
file that was left out of the graph, and a call whose target was skipped records an honest
"unknown" rather than a broken link.

Turn the flag on and the links come back.

---

## See also

- [User guide → choosing which files are analyzed](USER_GUIDE.md#choosing-which-files-are-analyzed)
- [Parser reference](parser-reference.md) — including how a new markup parser declares its
  template extensions
- `breezeai-cog capabilities` — the live, authoritative extension list
