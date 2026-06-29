"""Angular framework parser: config-object routes, lazy mounts, guards, selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_angular.parser import AngularParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import { RouterModule, Routes } from '@angular/router';
import { NgModule } from '@angular/core';

const routes: Routes = [
  { path: 'orders', component: OrderListComponent },
  { path: 'orders/:id', component: OrderDetailComponent, canActivate: [AuthGuard] },
  { path: 'admin', loadChildren: () => import('./admin/admin.module').then(m => m.AdminModule) },
  {
    path: 'settings',
    component: SettingsComponent,
    children: [
      { path: 'profile', component: ProfileComponent }
    ]
  }
];

@NgModule({ imports: [RouterModule.forRoot(routes)] })
export class AppRoutingModule {}
'''


def _parse(tmp_path) -> FileRecord:
    p = tmp_path / "app-routing.module.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="app-routing.module.ts", abs_path=p, source=SRC, repo_root=tmp_path)
    return AngularParser().parse_file(ctx)


def test_routes(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert {"/orders", "/orders/:id", "/admin", "/settings", "/settings/profile"} <= set(routes)
    assert routes["/orders"].handler == "OrderListComponent" and routes["/orders"].routeKind == "page"
    assert routes["/orders/:id"].guards == ["AuthGuard"]
    assert routes["/admin"].routeKind == "mount"  # loadChildren lazy mount
    assert routes["/settings/profile"].handler == "ProfileComponent"  # nested child path joined
    assert rec.framework == "angular"
    assert any(c.name == "AppRoutingModule" for c in rec.classes)  # base extraction reused


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_angular() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(AngularParser())
    assert registry.select("x.ts", b"import { Component } from '@angular/core';").name == "typescript-angular"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()
