"""TypeScript / JavaScript ``calls[].path`` names only a file that declares what is called
(BREEZEAI-1653, ``parsers/typescript/call_targets.py``): a re-export is followed to the declaring
file, a member of an imported value keeps the import's file only when that file declares it, and a
method is found on the class or its nearest in-repo ancestor."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records


@pytest.fixture
def calls_of(tmp_path: Path):
    """``{path: source}`` → ``{function name: [(call name, path), …]}`` over every file."""

    def run(files: dict[str, str]) -> dict[str, list[tuple[str, str | None]]]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        out: dict[str, list[tuple[str, str | None]]] = {}
        for rec in iter_file_records(repo):
            for fn in rec.functions:
                out[fn.name] = [(c.name, c.path) for c in fn.calls]
        return out

    return run


def test_barrel_import_resolves_to_the_declaring_file(calls_of) -> None:
    got = calls_of({
        "src/infra/storage.ts": "export function uploadFile(f: string) { return f; }\n",
        "src/infra/index.ts": "export { uploadFile } from './storage';\nexport * from './other';\n",
        "src/infra/other.ts": "export const getProvider = () => 1;\n",
        "src/app.ts": "import { uploadFile, getProvider } from './infra';\n"
                      "export function run() { uploadFile('x'); getProvider(); }\n",
    })
    assert ("uploadFile", "src/infra/storage.ts") in got["run"]
    assert ("getProvider", "src/infra/other.ts") in got["run"]  # through `export *`


def test_builtin_on_an_imported_constant_has_no_path(calls_of) -> None:
    got = calls_of({
        "src/modes.ts": "export const CHAT_MODES = ['normal', 'deep'] as const;\n"
                        "export function pick() { return 1; }\n",
        "src/app.ts": "import { CHAT_MODES } from './modes';\n"
                      "export function run() { return CHAT_MODES.join(', '); }\n",
    })
    assert ("join", None) in got["run"]


def test_method_of_an_imported_object_keeps_its_file(calls_of) -> None:
    # the twin: an object literal that does declare the method, and a singleton instance whose
    # class is in the same file
    got = calls_of({
        "src/api.ts": "export const api = { get(u: string) { return u; } };\n"
                      "class Svc { create() { return 1; } }\nexport const svc = new Svc();\n",
        "src/app.ts": "import { api, svc } from './api';\n"
                      "export function run() { api.get('/x'); svc.create(); }\n",
    })
    assert ("get", "src/api.ts") in got["run"]
    assert ("create", "src/api.ts") in got["run"]


def test_instance_of_a_library_object_has_no_path(calls_of) -> None:
    got = calls_of({
        "src/db/index.ts": "import { Sequelize } from 'sequelize';\n"
                           "const db = new Sequelize('x');\nexport default db;\n",
        "src/app.ts": "import db from './db';\nexport async function run() { await db.transaction(); }\n",
    })
    assert ("transaction", None) in got["run"]


def test_inherited_method_resolves_to_the_ancestor_file(calls_of) -> None:
    got = calls_of({
        "src/base.service.ts": "export class BaseService { getRepository() { return 1; } }\n",
        "src/document.service.ts": "import { BaseService } from './base.service';\n"
                                   "export class DocumentService extends BaseService {\n"
                                   "  load() { return 2; }\n}\n",
        "src/diff.service.ts": "import { DocumentService } from './document.service';\n"
                               "export class DiffService {\n"
                               "  constructor(private documentService: DocumentService) {}\n"
                               "  run() { this.documentService.getRepository(); this.documentService.load(); }\n"
                               "}\n",
    })
    assert ("getRepository", "src/base.service.ts") in got["run"]
    assert ("load", "src/document.service.ts") in got["run"]


def test_library_base_method_has_no_path(calls_of) -> None:
    # `find` comes from a library base class: no in-repo file declares it
    got = calls_of({
        "src/order.repository.ts": "import { DefaultCrudRepository } from '@loopback/repository';\n"
                                   "export class OrderRepository extends DefaultCrudRepository {\n"
                                   "  custom() { return 1; }\n}\n",
        "src/order.service.ts": "import { OrderRepository } from './order.repository';\n"
                                "export class OrderService {\n"
                                "  constructor(private repo: OrderRepository) {}\n"
                                "  run() { this.repo.find(); this.repo.custom(); }\n}\n",
    })
    assert ("find", None) in got["run"]
    assert ("custom", "src/order.repository.ts") in got["run"]


def test_arrow_field_method_is_declared(calls_of) -> None:
    got = calls_of({
        "src/svc.ts": "export class Svc { save = async (x: number) => x; }\n",
        "src/app.ts": "import { Svc } from './svc';\n"
                      "export class App {\n  constructor(private svc: Svc) {}\n"
                      "  run() { this.svc.save(1); }\n}\n",
    })
    assert ("save", "src/svc.ts") in got["run"]


def test_namespace_import_follows_the_module_export(calls_of) -> None:
    got = calls_of({
        "src/util/dates.ts": "export function formatDate(d: Date) { return d; }\n",
        "src/util/index.ts": "export * from './dates';\n",
        "src/app.ts": "import * as util from './util';\n"
                      "export function run() { util.formatDate(new Date()); util.missing(); }\n",
    })
    assert ("formatDate", "src/util/dates.ts") in got["run"]
    assert ("missing", None) in got["run"]


def test_static_method_of_an_imported_class(calls_of) -> None:
    got = calls_of({
        "src/base.ts": "export class Base { static of() { return 1; } }\n",
        "src/child.ts": "import { Base } from './base';\nexport class Child extends Base {}\n",
        "src/app.ts": "import { Child } from './child';\n"
                      "export function run() { Child.of(); Child.nope(); }\n",
    })
    assert ("of", "src/base.ts") in got["run"]
    assert ("nope", None) in got["run"]


def test_imported_value_that_is_no_function_has_no_path(calls_of) -> None:
    got = calls_of({
        "src/store.ts": "import { defineStore } from 'pinia';\n"
                        "export const useStore = defineStore('s', { state: () => ({}) });\n",
        "src/fn.ts": "export const helper = (x: number) => x;\n",
        "src/app.ts": "import { useStore } from './store';\nimport { helper } from './fn';\n"
                      "export function run() { useStore(); helper(1); }\n",
    })
    assert ("useStore", None) in got["run"]
    assert ("helper", "src/fn.ts") in got["run"]


def test_commonjs_module_object(calls_of) -> None:
    got = calls_of({
        "src/lib.js": "exports.ping = function () { return 1; };\n",
        "src/app.js": "const lib = require('./lib');\n"
                      "function run() { lib.ping(); lib.pong(); }\nmodule.exports = { run };\n",
    })
    assert ("ping", "src/lib.js") in got["run"]
    assert ("pong", None) in got["run"]


def test_same_file_and_this_calls_unchanged(calls_of) -> None:
    got = calls_of({
        "src/a.ts": "export class A {\n  one() { return this.two(); }\n  two() { return helper(); }\n}\n"
                    "function helper() { return 1; }\n",
    })
    assert ("two", "src/a.ts") in got["one"]
    assert ("helper", "src/a.ts") in got["two"]


def test_interface_and_abstract_methods_keep_their_file(calls_of) -> None:
    # the parser emits Functions for signatures: a call through the interface or the abstract
    # base type keeps that file
    got = calls_of({
        "src/queue.interface.ts": "export interface Queue { deleteMessage(id: string): void; }\n",
        "src/base.ts": "export abstract class Base { abstract handle(): void; }\n",
        "src/app.ts": "import { Queue } from './queue.interface';\nimport { Base } from './base';\n"
                      "export class App {\n  constructor(private q: Queue, private b: Base) {}\n"
                      "  run() { this.q.deleteMessage('x'); this.b.handle(); }\n}\n",
    })
    assert ("deleteMessage", "src/queue.interface.ts") in got["run"]
    assert ("handle", "src/base.ts") in got["run"]


def test_value_built_by_a_call_is_no_function(calls_of) -> None:
    # `create(…)` / `memo(…)` return a value the parser emits no Function for
    got = calls_of({
        "src/store.ts": "import { create } from 'zustand';\n"
                        "export const useCart = create((set) => ({ n: 0 }));\n",
        "src/app.ts": "import { useCart } from './store';\nexport function run() { useCart(); }\n",
    })
    assert ("useCart", None) in got["run"]
