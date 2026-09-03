"""Vue framework parser: one parser for Vue 2 + Vue 3. Covers ``.vue`` SFC script
extraction (shadow source preserves line numbers), vue-router config routes for both
``createRouter`` (v3/vue-router 4) and ``new VueRouter`` (v2/vue-router 3), selection,
and schema conformance."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_vue.parser import VueParser
from breezeai_cog.schemas import FileRecord


def _parse(path: str, source: bytes, tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / path.rsplit("/", 1)[-1]
    p.write_bytes(source)
    ctx = ParseContext(
        path=path, abs_path=p, source=source, repo_root=tmp_path, capture_statements=capture
    )
    return VueParser().parse_file(ctx)


# ── vue-router route detection (Vue 3: createRouter / vue-router 4) ─────────────

_V3_ROUTER = b"""import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'

const routes = [
  { path: '/', name: 'home', component: Home },
  { path: '/reports', name: 'reports', component: () => import('../views/Reports.vue') },
  { path: '/settings', component: () => import('../views/Settings.vue'), children: [
    { path: 'profile', name: 'profile', component: () => import('../views/Profile.vue') },
  ]},
  { path: '/old', redirect: '/' },
]

export default createRouter({ history: createWebHistory(), routes })
"""


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with --capture-statements.
    rec = _parse("src/router/index.ts", _V3_ROUTER, tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    # framework is the parser's identity (set unconditionally on any file it claims), not a
    # route-detection by-product — so a vue-importing router config is "vue" even uncaptured.
    assert rec.framework == "vue"


def test_vue3_routes(tmp_path) -> None:
    rec = _parse("src/router/index.ts", _V3_ROUTER, tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/", "/reports", "/settings", "/settings/profile"} <= set(routes)
    assert rec.framework == "vue"
    # bare-identifier component -> its name; every route is a page (no mount concept)
    assert routes["/"].handler == "Home" and routes["/"].routeKind == "page"
    # nested child path joins onto the parent
    assert routes["/settings/profile"].handler == "../views/Profile.vue"


def test_lazy_import_handler_is_page(tmp_path) -> None:
    # component: () => import('./X.vue') is a lazily-loaded PAGE (vue-router has no
    # Angular-style loadChildren mount) — handler is the resolvable import specifier.
    rec = _parse("src/router/index.ts", _V3_ROUTER, tmp_path)
    reports = next(
        s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/reports"
    )
    assert reports.routeKind == "page"
    assert reports.handler == "../views/Reports.vue"


def test_name_key_not_excluded(tmp_path) -> None:
    # Vue routes routinely carry a top-level `name` — unlike Angular (where `name` marked a
    # breadcrumb object), a named route MUST still be captured.
    rec = _parse("src/router/index.ts", _V3_ROUTER, tmp_path)
    named = next(
        (s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/reports"),
        None,
    )
    assert named is not None, "a named Vue route must not be dropped"


def test_redirect_routes_skipped(tmp_path) -> None:
    # A redirect entry is an alias, not a page/endpoint — it must not be emitted.
    rec = _parse("src/router/index.ts", _V3_ROUTER, tmp_path)
    assert not any(s.semanticType == "route" and s.endpoint == "/old" for s in rec.statements)


# ── Gap B: layout route with component + redirect + children ───────────────────

_LAYOUT_ROUTER = b"""import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '../layouts/AppLayout.vue'

const routes = [
  { path: '/:pathMatch(.*)*', redirect: { name: 'dashboard' } },
  {
    name: 'admin', path: '/', component: AppLayout, redirect: { name: 'dashboard' },
    children: [
      { path: 'dashboard', component: () => import('../pages/Dashboard.vue') },
      { path: 'users', component: () => import('../pages/Users.vue') },
      { path: 'payments', component: RouteView, children: [
        { path: 'billing', component: () => import('../pages/Billing.vue') },
      ]},
    ],
  },
]
export default createRouter({ history: createWebHistory(), routes })
"""


def test_layout_route_children_recursed(tmp_path) -> None:
    # A parent with component + redirect + children must NOT drop its subtree (Gap B): the
    # children (and grandchildren) are the whole app, joined onto the parent's path.
    rec = _parse("src/router/index.ts", _LAYOUT_ROUTER, tmp_path)
    eps = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert {"/", "/dashboard", "/users", "/payments", "/payments/billing"} <= eps
    # the parent has a real component → emitted as a page (handler = the layout)
    root = next(s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/")
    assert root.handler == "AppLayout"


def test_pure_redirect_still_skipped(tmp_path) -> None:
    # A redirect with NO component of its own is an alias — still never emitted, even though
    # the fix now recurses through redirect-bearing nodes.
    rec = _parse("src/router/index.ts", _LAYOUT_ROUTER, tmp_path)
    eps = [s.endpoint for s in rec.statements if s.semanticType == "route"]
    # the top-level `{ path: '/:pathMatch(.*)*', redirect }` (no component) must be absent
    assert "/:pathMatch(.*)*" not in eps


# ── Vue 2 form (new VueRouter / vue-router 3) — SAME detector, one parser ───────

_V2_ROUTER = b"""import Vue from 'vue'
import VueRouter from 'vue-router'
import UserList from '@/views/UserList.vue'

Vue.use(VueRouter)

const routes = [
  { path: '/users', name: 'users', component: UserList },
  { path: '/about', component: () => import('@/views/About.vue') },
]

export default new VueRouter({ mode: 'history', routes })
"""


def test_vue2_routes_same_detector(tmp_path) -> None:
    # The Vue 2 `new VueRouter({ routes })` form uses the identical route-array shape —
    # no separate parser, no separate detector.
    rec = _parse("src/router.js", _V2_ROUTER, tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/users", "/about"} <= set(routes)
    assert routes["/users"].handler == "UserList" and routes["/users"].framework == "vue"
    assert routes["/about"].handler == "@/views/About.vue"


# ── .vue SFC script extraction (shadow source) ─────────────────────────────────

_SFC = b"""<template>
  <button @click="save">{{ label }}</button>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import UserList from './UserList.vue'

const label = ref('Save')

function save() {
  fetch('/api/save', { method: 'POST' })
}
</script>

<style scoped>
button { color: red; }
</style>
"""


def test_sfc_captures_script_only(tmp_path) -> None:
    rec = _parse("src/components/Widget.vue", _SFC, tmp_path)
    assert "save" in [f.name for f in rec.functions]
    assert "vue" in rec.externalImports and "./UserList.vue" in rec.externalImports


def test_sfc_line_numbers_preserved(tmp_path) -> None:
    # The shadow-source trick keeps every span at its REAL .vue line: `fetch` is on line 12
    # of the file, not line 2 of the extracted <script> block.
    rec = _parse("src/components/Widget.vue", _SFC, tmp_path)
    api = next(s for s in rec.statements if s.semanticType == "api_call")
    assert api.startLine == 12, f"expected line 12, got {api.startLine}"
    save = next(f for f in rec.functions if f.name == "save")
    assert save.startLine == 11


def test_sfc_language_is_script_lang_and_framework_is_vue(tmp_path) -> None:
    # Two orthogonal axes: `language` is the script's JS/TS lang (here `<script lang="ts">`
    # -> typescript, NOT "vue" — the extension can't carry the JS/TS fact); `framework` is
    # the framework identity (vue). `uiRole` (component) is the third axis.
    rec = _parse("src/components/Widget.vue", _SFC, tmp_path)
    assert rec.language == "typescript"
    assert rec.framework == "vue"
    assert rec.uiRole == "component"


def test_sfc_language_javascript_without_lang_ts(tmp_path) -> None:
    # A plain `<script>` (or `lang="js"`) SFC is javascript on the language axis.
    src = b"<template><p>{{ x }}</p></template>\n<script>\nexport default { data: () => ({ x: 1 }) }\n</script>\n"
    rec = _parse("src/components/JsWidget.vue", src, tmp_path)
    assert rec.language == "javascript" and rec.framework == "vue"


def test_template_only_sfc(tmp_path) -> None:
    # An SFC with no <script> block has no code to capture — a minimal, valid record. No
    # script -> javascript on the language axis (the type-free baseline; never guess TS).
    src = b"<template>\n  <p>only markup</p>\n</template>\n<style>.p {}</style>\n"
    rec = _parse("src/components/Static.vue", src, tmp_path)
    assert rec.language == "javascript" and rec.framework == "vue"
    assert rec.functions == [] and rec.statements == []


def test_sfc_marked_as_component(tmp_path) -> None:
    # Every .vue SFC is a component — File carries uiRole="component".
    assert _parse("src/components/Widget.vue", _SFC, tmp_path).uiRole == "component"


def test_template_only_sfc_is_component(tmp_path) -> None:
    # The empty/template-only SFC path also carries the marker.
    src = b"<template><p>x</p></template>\n"
    assert _parse("src/components/Static.vue", src, tmp_path).uiRole == "component"


def test_vue_claimed_ts_is_not_a_component(tmp_path) -> None:
    # A .ts file the Vue parser also claims (a router/config that imports vue) is NOT a
    # component — the marker is gated on the .vue extension.
    src = b"import { createRouter } from 'vue-router'\nexport const r = createRouter({})\n"
    rec = _parse("src/router.ts", src, tmp_path)
    assert rec.language == "typescript" and rec.uiRole is None


# ── defineComponent components ───────────────────────────────────────────────────


def _stmt_roles(rec):
    return {s.name: s.uiRole for s in rec.statements if s.nodeType == "lexical_declaration"}


def test_default_export_definecomponent_marks_file(tmp_path) -> None:
    # `export default defineComponent(...)` — one component per module → File.uiRole.
    src = b"import { defineComponent } from 'vue'\nexport default defineComponent({ setup(){ const n = 1 } })\n"
    rec = _parse("src/Comp.ts", src, tmp_path)
    assert rec.uiRole == "component"
    assert _stmt_roles(rec).get("n") is None  # a nested local is not a component


def test_named_definecomponent_marks_statement(tmp_path) -> None:
    # `export const X = …` (object) and `const Y = …` (functional) → the lexical_declaration
    # statement carries uiRole; the File does not (multiple components can share a module).
    src = (
        b"import { defineComponent } from 'vue'\n"
        b"export const X = defineComponent({ setup(){} })\n"
        b"const Y = defineComponent(() => () => null)\n"
    )
    rec = _parse("src/Comps.ts", src, tmp_path)
    assert rec.uiRole is None
    assert _stmt_roles(rec) == {"X": "component", "Y": "component"}


def test_definecomponent_import_alias_resolved(tmp_path) -> None:
    # `import { defineComponent as dc }` — the alias is resolved from the import specifier.
    src = b"import { defineComponent as dc } from 'vue'\nexport const Z = dc({ setup(){} })\n"
    assert _stmt_roles(_parse("src/Z.ts", src, tmp_path)) == {"Z": "component"}


def test_local_definecomponent_not_from_vue_not_marked(tmp_path) -> None:
    # A local function coincidentally named defineComponent (not imported from vue) is NOT a
    # component — detection is keyed on the real vue import, not the bare callee text.
    src = b"function defineComponent(x){ return x }\nexport const W = defineComponent({})\n"
    assert _stmt_roles(_parse("src/W.ts", src, tmp_path)).get("W") is None


# ── defineStore (Pinia) stores ───────────────────────────────────────────────────


def test_definestore_marks_statement_as_store(tmp_path) -> None:
    # Pinia stores (setup and options form) → the lexical_declaration statement gets uiRole=store.
    src = (
        b"import { defineStore } from 'pinia'\n"
        b"export const useUserStore = defineStore('user', () => ({}))\n"
        b"export const useCart = defineStore('cart', { state: () => ({}) })\n"
    )
    assert _stmt_roles(_parse("src/store.ts", src, tmp_path)) == {
        "useUserStore": "store",
        "useCart": "store",
    }


def test_definestore_default_export_marks_file(tmp_path) -> None:
    src = b"import { defineStore } from 'pinia'\nexport default defineStore('x', {})\n"
    assert _parse("src/s.ts", src, tmp_path).uiRole == "store"


def test_definestore_alias_and_non_pinia(tmp_path) -> None:
    # alias resolved; a local defineStore not imported from pinia is not marked.
    aliased = b"import { defineStore as ds } from 'pinia'\nexport const useX = ds('x', {})\n"
    assert _stmt_roles(_parse("src/a.ts", aliased, tmp_path)) == {"useX": "store"}
    local = b"function defineStore(a, b){ return b }\nexport const useY = defineStore('y', {})\n"
    assert _stmt_roles(_parse("src/b.ts", local, tmp_path)).get("useY") is None


def test_component_and_store_in_one_file(tmp_path) -> None:
    # Both factories resolve to their own roles within a single module.
    src = (
        b"import { defineComponent } from 'vue'\n"
        b"import { defineStore } from 'pinia'\n"
        b"export const C = defineComponent({})\n"
        b"export const useS = defineStore('s', {})\n"
    )
    assert _stmt_roles(_parse("src/m.ts", src, tmp_path)) == {"C": "component", "useS": "store"}


# ── composables ──────────────────────────────────────────────────────────────────


def _fn_roles(rec):
    return {f.name: f.uiRole for f in rec.functions}


def test_composable_marked_by_reactivity_call(tmp_path) -> None:
    # A `useX` function that calls a Vue reactivity primitive → uiRole="composable".
    # A `useX` util with no reactivity is NOT marked, and a component's setup (not useX) is not.
    src = (
        b"import { ref, computed } from 'vue'\n"
        b"import { defineComponent } from 'vue'\n"
        b"export function useAuth(){ const u = ref(null); const x = computed(() => u.value); return { u, x } }\n"
        b"export function useLegacyConfig(k){ return CFG[k] }\n"
        b"export const C = defineComponent({ setup(){ const n = ref(0); return { n } } })\n"
    )
    roles = _fn_roles(_parse("src/c.ts", src, tmp_path))
    assert roles.get("useAuth") == "composable"
    assert roles.get("useLegacyConfig") is None      # useX but no reactive state
    assert roles.get("setup") is None                # reactivity, but not a useX composable


def test_composable_arrow_and_alias(tmp_path) -> None:
    arrow = b"import { reactive } from 'vue'\nexport const useStore2 = () => { const s = reactive({}); return s }\n"
    assert _fn_roles(_parse("src/a.ts", arrow, tmp_path)).get("useStore2") == "composable"
    aliased = b"import { ref as r } from 'vue'\nexport function useThing(){ const v = r(0); return v }\n"
    assert _fn_roles(_parse("src/b.ts", aliased, tmp_path)).get("useThing") == "composable"


def test_composable_local_reactivity_not_from_vue(tmp_path) -> None:
    # A local `ref` (not imported from vue) must not make a useX function a composable.
    src = b"function ref(x){ return x }\nexport function useX(){ const v = ref(1); return v }\n"
    assert _fn_roles(_parse("src/n.ts", src, tmp_path)).get("useX") is None


# ── selection ──────────────────────────────────────────────────────────────────


def test_claims_selection() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(VueParser())
    # a .vue file is always Vue's
    assert registry.select("x.vue", b"<template></template>").name == "typescript-vue"
    # a .ts file that imports vue-router / vue is Vue's
    assert registry.select("router.ts", _V3_ROUTER).name == "typescript-vue"
    assert registry.select("app.ts", b"import { createApp } from 'vue'\n").name == "typescript-vue"
    # plain TS falls back to the base parser
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"
    # `'vuex'` must not false-match the `'vue'` guard
    assert registry.select("x.ts", b"import s from 'vuex'\n").name == "typescript"
    registry.clear()


# ── negative: non-route data arrays ────────────────────────────────────────────


def test_nav_array_not_captured(tmp_path) -> None:
    # A data array with `path` but no router-discriminating key (a nav menu) must not be
    # captured as routes — even though the file references vue-router (byte guard fires).
    src = b"""import { useRouter } from 'vue-router'
const menu = [
  { path: '/home', label: 'Home' },
  { path: '/about', label: 'About' },
]
export default menu
"""
    rec = _parse("src/nav.ts", src, tmp_path)
    assert not any(s.semanticType == "route" for s in rec.statements)


# ── Gap A: route arrays in files with NO vue-router import ─────────────────────


def _parse_base(path: str, source: bytes, tmp_path) -> FileRecord:
    # Parse with the BASE TypeScriptParser (not VueParser) — proves vue-route detection runs
    # in the base additive pass, so a signal-less file the VueParser never claims is covered.
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    p = tmp_path / path.rsplit("/", 1)[-1]
    p.write_bytes(source)
    ctx = ParseContext(
        path=path, abs_path=p, source=source, repo_root=tmp_path, capture_statements=True
    )
    return TypeScriptParser().parse_file(ctx)


# The vue2-elm shape: a route array as the default export, in a file importing nothing from
# vue-router (the `new VueRouter({ routes })` lives elsewhere). Before the fix: 0 routes.
_SIGNALLESS_ROUTES = b"""import App from '../App'
const home = () => import('../page/home/home')

export default [{
  path: '/', component: App,
  children: [
    { path: '', redirect: '/home' },
    { path: '/home', component: home },
    { path: '/city/:cityid', component: City },
  ],
}]
"""


def test_signalless_route_array_captured_by_base_parser(tmp_path) -> None:
    rec = _parse_base("src/router/router.js", _SIGNALLESS_ROUTES, tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/", "/home", "/city/:cityid"} <= set(routes)
    assert routes["/home"].handler == "home" and routes["/home"].framework == "vue"
    assert rec.framework == "vue"
    # the redirect-only child is still skipped
    assert not any(s.endpoint == "" for s in rec.statements if s.semanticType == "route")


def test_signalless_detection_needs_capture_statements(tmp_path) -> None:
    p = tmp_path / "router.js"
    p.write_bytes(_SIGNALLESS_ROUTES)
    ctx = ParseContext(
        path="src/router/router.js",
        abs_path=p,
        source=_SIGNALLESS_ROUTES,
        repo_root=tmp_path,
        capture_statements=False,
    )
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    rec = TypeScriptParser().parse_file(ctx)
    assert not any(s.semanticType == "route" for s in rec.statements)


def test_angular_react_config_not_double_emitted(tmp_path) -> None:
    # Angular/React use the SAME {path, component} array shape. The additive Vue pass must
    # defer to them (they have their own detectors) — never emit a framework="vue" duplicate.
    ng = b"""import { Routes } from '@angular/router';
export const routes: Routes = [ { path: 'x', component: XComponent } ];
"""
    rec = _parse_base("app.routes.ts", ng, tmp_path)
    assert not any(s.framework == "vue" for s in rec.statements)


def test_file_tree_with_path_children_not_captured(tmp_path) -> None:
    # {path, children} with NO component is a file-tree / menu shape — the tightened
    # discriminator must not treat it as a route array (false-positive guard for Gap A).
    src = b"""export const tree = [
  { path: '/root', children: [ { path: '/root/a', children: [] } ] },
]
"""
    rec = _parse_base("src/tree.ts", src, tmp_path)
    assert not any(s.semanticType == "route" for s in rec.statements)


# ── single-object route modules (const r = {…}; export default r) ──────────────

_MODULE_ROUTE = b"""import Layout from '@/layout'

const tableRouter = {
  path: '/table',
  component: Layout,
  redirect: '/table/complex-table',
  name: 'Table',
  children: [
    { path: 'complex-table', component: () => import('@/views/table/complex.vue') },
    { path: 'inline-edit', component: () => import('@/views/table/inline.vue') },
  ],
}

export default tableRouter
"""


def test_single_object_route_module_captured(tmp_path) -> None:
    # A router/modules/*.js fragment exports ONE route object (not an array). It must be
    # captured with its children, even though it imports nothing from vue-router.
    rec = _parse_base("src/router/modules/table.js", _MODULE_ROUTE, tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/table", "/table/complex-table", "/table/inline-edit"} <= set(routes)
    assert routes["/table"].handler == "Layout"  # component + redirect → still a page (Gap B)


def test_object_route_not_double_emitted_via_identifier(tmp_path) -> None:
    # When the module object is ALSO referenced by identifier inside a routes array, it must
    # be emitted exactly once (from its declaration), not twice.
    src = _MODULE_ROUTE + b"\nconst routes = [tableRouter]\n"
    rec = _parse_base("src/router/index.js", src, tmp_path)
    table = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/table"]
    assert len(table) == 1


# ── mock / fixture directories are not route sources ───────────────────────────


def test_mock_dir_routes_skipped(tmp_path) -> None:
    # Route-shaped data under a mock/ directory is a dev stand-in, not the wired router —
    # route emission must skip it (the file is still structurally parsed).
    rec = _parse_base("mock/role/routes.js", _SIGNALLESS_ROUTES, tmp_path)
    assert not any(s.semanticType == "route" for s in rec.statements)


def test_mock_basename_skipped(tmp_path) -> None:
    # A *.mock.ts file (even outside a mock/ dir) is a fixture — no routes emitted.
    src = b"""import { createRouter } from 'vue-router'
export const routes = [ { path: '/x', component: X } ]
"""
    rec = _parse_base("src/api/menu.mock.ts", src, tmp_path)
    assert not any(s.semanticType == "route" for s in rec.statements)


def test_non_mock_dir_still_captured(tmp_path) -> None:
    # Segment-exact match: a 'mockups' directory is NOT a fixture dir — routes still captured.
    rec = _parse_base("src/mockups/router.js", _SIGNALLESS_ROUTES, tmp_path)
    assert any(s.semanticType == "route" for s in rec.statements)


# ── schema conformance ─────────────────────────────────────────────────────────


def test_output_validates(tmp_path) -> None:
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    for path, src in [
        ("src/router/index.ts", _V3_ROUTER),
        ("src/router.js", _V2_ROUTER),
        ("src/components/Widget.vue", _SFC),
    ]:
        rec = _parse(path, src, tmp_path)
        errors = list(validator.iter_errors(json.loads(to_line(rec))))
        assert not errors, (path, errors)
