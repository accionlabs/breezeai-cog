"""Programmatic navigation and React <Link> as outbound navigation (the ``navigation`` detectors
in parsers/typescript_vue, typescript_angular and typescript_react)."""

from __future__ import annotations

PKG = '{"dependencies": {"vue": "3", "vue-router": "4", "react": "18", "react-router-dom": "6"}}'


def _nav(stmts, path: str) -> list[tuple[str, str | None]]:
    return sorted((s.framework, s.endpoint or "")
                  for s in stmts if s.routeKind == "navigation" and s.path == path)


def test_vue_push_forms(analyze_files) -> None:
    src = (
        "import { useRouter } from 'vue-router';\n"
        "export function go(id) {\n  const router = useRouter();\n"
        "  router.push({ path: '/orders' });\n"
        "  router.push({ name: 'order', params: { id } });\n"   # a named route: no address
        "  router.replace('details');\n"                         # relative: depends on the page
        "}\n"
    )
    st = analyze_files({"package.json": PKG, "src/go.js": src})
    assert _nav(st, "src/go.js") == [("vue", ""), ("vue", ""), ("vue", "/orders")]


def test_angular_link_array(analyze_files) -> None:
    svc = (
        "import { Injectable } from '@angular/core';\nimport { Router } from '@angular/router';\n"
        "@Injectable()\nexport class Go {\n  constructor(private readonly router: Router) {}\n"
        "  a() { this.router.navigate(['orders', 'new']); }\n"           # absolute from root
        "  b(id: string) { this.router.navigate(['/orders', id]); }\n"   # a variable segment
        "  c() { this.router.navigate(['x'], { relativeTo: this.route }); }\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/app/go.ts": svc})
    assert _nav(st, "src/app/go.ts") == [("angular", ""), ("angular", ""), ("angular", "/orders/new")]


def test_a_call_nested_in_a_larger_statement_gets_its_own_record(analyze_files) -> None:
    src = (
        "import React from 'react';\nimport { useNavigate } from 'react-router-dom';\n"
        "export function Go() {\n  const navigate = useNavigate();\n"
        "  return (\n    <button onClick={() => navigate('/reports')}>Go</button>\n  );\n}\n"
    )
    st = analyze_files({"package.json": PKG, "src/Go.tsx": src})
    [r] = [s for s in st if s.routeKind == "navigation"]
    assert (r.startLine, r.endpoint, r.text) == (6, "/reports", "navigate('/reports')")


def test_aliased_link_and_unrelated_push(analyze_files) -> None:
    src = (
        "import React from 'react';\nimport { NavLink as Tab } from 'react-router-dom';\n"
        "const items = [];\nitems.push('/not-navigation');\n"
        'export const T = () => <Tab to="/settings">Settings</Tab>;\n'
    )
    st = analyze_files({"package.json": PKG, "src/T.tsx": src})
    assert _nav(st, "src/T.tsx") == [("react", "/settings")]
