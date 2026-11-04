"""Resolver objects fact (parsers/typescript_graphql/resolver_objects.py): an object of functions."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from breezeai_cog.parsers.typescript.imports import build_ts_index
from breezeai_cog.parsers.typescript_graphql.resolver_objects import resolver_object


def _index(tmp_path: Path, files: dict[str, str], jobs: int):
    paths = []
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        paths.append(f)
    return build_ts_index(tmp_path, paths, jobs)


@pytest.mark.parametrize("jobs", [1, 2])
def test_members_carry_the_handler_and_the_line_of_each_function(tmp_path, jobs) -> None:
    index = _index(
        tmp_path,
        {
            "src/queries.ts": "export const queries = {\n"
            "  books: async () => [],\n"
            "  book(_, { id }) { return null; },\n"
            "  authors: getAuthors,\n"
            "  tags: wrap(() => []),\n"
            "  'last-one': function () { return null; },\n"
            "};\n",
            "src/subs.ts": "export const subs = {\n"
            "  added: { subscribe: () => null },\n"
            "  removed: { resolve: (p) => p, subscribe: function () {} },\n"
            "};\n",
        },
        jobs,
    )
    assert resolver_object(index, "src/queries.ts", "queries") == (
        ("books", None, 2),
        ("book", "book", 3),
        ("authors", None, None),  # a reference: declared elsewhere, so no name and no line
        ("tags", None, None),  # a wrapper call
        ("last-one", None, 6),
    )
    assert resolver_object(index, "src/subs.ts", "subs") == (
        ("added", None, 2),
        ("removed", None, 3),
    )


def test_a_value_wrapped_in_as_const_or_satisfies_is_read(tmp_path) -> None:
    index = _index(
        tmp_path,
        {
            "src/a.ts": "export const a = {\n  x: () => 1,\n} as const;\n",
            "src/b.ts": "export const b = {\n  y: () => 1,\n} satisfies Resolvers;\n",
        },
        1,
    )
    assert resolver_object(index, "src/a.ts", "a") == (("x", None, 2),)
    assert resolver_object(index, "src/b.ts", "b") == (("y", None, 2),)


@pytest.mark.parametrize(
    "source",
    [
        "export const o = { n: 1, f: () => 1 };\n",  # a literal property
        "export const o = { f: () => 1, list: [1] };\n",  # an array property
        "export const o = { f: () => 1, nested: { a: 1 } };\n",  # a plain object property
        "export const o = {};\n",
        "export const o = make();\n",
        "export const o = () => 1;\n",
    ],
)
def test_an_object_with_a_data_property_or_no_function_is_no_resolver_object(
    tmp_path, source
) -> None:
    index = _index(tmp_path, {"src/o.ts": source}, 1)
    assert resolver_object(index, "src/o.ts", "o") == ()


def test_the_object_is_keyed_by_its_declared_name_exported_or_not(tmp_path) -> None:
    # `export { hidden as shown }`: the declaration's name is what the exports fact resolves to.
    index = _index(
        tmp_path,
        {
            "src/h.ts": "const hidden = {\n  f: () => 1,\n};\nexport { hidden as shown };\n",
        },
        1,
    )
    assert resolver_object(index, "src/h.ts", "hidden") == (("f", None, 2),)
    assert resolver_object(index, "src/h.ts", "shown") == ()


def test_a_name_declared_twice_is_left_out(tmp_path) -> None:
    index = _index(
        tmp_path,
        {
            "src/d.ts": "export const o = { f: () => 1 };\nvar o = { g: () => 2 };\n",
        },
        1,
    )
    assert resolver_object(index, "src/d.ts", "o") == ()


def test_unknown_file_or_name_gives_nothing(tmp_path) -> None:
    index = _index(tmp_path, {"src/q.ts": "export const q = { f: () => 1 };\n"}, 1)
    assert resolver_object(index, "src/missing.ts", "q") == ()
    assert resolver_object(index, "src/q.ts", "other") == ()
    assert resolver_object(None, "src/q.ts", "q") == ()


def test_fact_crosses_the_process_boundary(tmp_path) -> None:
    index = _index(tmp_path, {"src/q.ts": "export const q = {\n  f: () => 1,\n};\n"}, 1)
    assert resolver_object(pickle.loads(pickle.dumps(index)), "src/q.ts", "q") == (("f", None, 2),)
