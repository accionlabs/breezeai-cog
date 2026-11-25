"""TypeScript / JavaScript generator functions and abstract methods are Functions: a generator in
every shape (declaration, `async function*`, a stored `function*`, a method `*m()` / `async *m()`),
and an abstract class's `abstract m(): T;` — the same contract shape as an interface method."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return repo


def test_generators_are_functions_with_their_own_statements(tmp_path: Path) -> None:
    repo = _write(tmp_path, {"src/a.ts": (
        "function *chunkIter<T>(iter: Iterable<T>) {\n  for (const x of iter) { yield x; }\n}\n"
        "export async function* readBundle(input: string) {\n  yield input.trim();\n}\n"
        "export const lazy = function* gen() { yield 1; };\n"
        "export class R { async *rows() { yield 1; } }\n"
        "export function main() { return chunkIter([1]); }\n"
    )})
    (rec,) = list(iter_file_records(repo))
    fns = {f.name: f for f in rec.functions}
    assert {"chunkIter", "readBundle", "lazy", "rows", "main"} <= set(fns)
    assert fns["lazy"].alias == "gen"
    assert ("chunkIter", "src/a.ts") in [(c.name, c.path) for c in fns["main"].calls]
    owner = {s.startLine: s.parentId for s in rec.statements}
    assert owner[2] == fns["chunkIter"].id  # the loop belongs to the generator, not the file
    assert owner[5] == fns["readBundle"].id
    # the binding of a stored generator is not repeated as a file statement (Rule 1)
    assert not [s for s in rec.statements if s.parentId == rec.id and s.startLine == 7]


def test_abstract_method_is_a_function_and_a_call_target(tmp_path: Path) -> None:
    repo = _write(tmp_path, {
        "src/base.ts": "export abstract class Base {\n  abstract renderSet(): Promise<void>;\n"
                       "  protected abstract label(x: string): string;\n}\n",
        "src/main.ts": "import { Base } from './base';\n"
                       "export async function main(r: Base) { await r.renderSet(); }\n",
    })
    recs = {r.path: r for r in iter_file_records(repo)}
    base = {f.name: f for f in recs["src/base.ts"].functions}
    assert base["renderSet"].returnType == "Promise<void>" and base["renderSet"].calls == []
    assert base["label"].visibility == "protected"
    (main,) = recs["src/main.ts"].functions
    assert ("renderSet", "src/base.ts") in [(c.name, c.path) for c in main.calls]
