"""The ``angularjs.registrations`` fact: built once in the index, the same for any ``--jobs``."""

from __future__ import annotations

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.core import pipeline
from breezeai_cog.emit.sinks import MemorySink


@pytest.mark.parametrize("jobs", [1, 2])
def test_route_reaches_a_controller_in_another_file_for_any_jobs(tmp_path, jobs) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.js").write_text(
        "angular.module('m').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: 'ACtrl' });\n"
        "});\n"
    )
    for i in range(3):  # enough files that a pool really splits them
        (repo / f"c{i}.js").write_text(
            f"angular.module('m').controller('Ctrl{i}', function () {{}});\n"
        )
    (repo / "a.js").write_text("\n\nangular.module('m').controller('ACtrl', function () {});\n")
    sink = MemorySink()
    pipeline.run(repo, Settings(jobs=jobs), sink)
    [route] = [s for r in sink.records for s in r.statements if s.semanticType == "route"]
    assert (route.handler, route.handlerPath, route.handlerLine) == ("ACtrl", "a.js", 3)


@pytest.mark.parametrize("jobs", [1, 2])
def test_child_state_reaches_a_parent_in_another_file_for_any_jobs(tmp_path, jobs) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "parent.js").write_text(
        "$stateProvider.state('app', { abstract: true, url: '/app' });\n"
    )
    (repo / "child.js").write_text("$stateProvider.state('app.users', { url: '/users' });\n")
    for i in range(3):
        (repo / f"s{i}.js").write_text(f"$stateProvider.state('s{i}', {{ url: '/s{i}' }});\n")
    sink = MemorySink()
    pipeline.run(repo, Settings(jobs=jobs), sink)
    got = sorted(
        s.endpoint for r in sink.records for s in r.statements if s.semanticType == "route"
    )
    assert got == ["/app/users", "/s0", "/s1", "/s2"]


@pytest.mark.parametrize("jobs", [1, 2])
def test_imported_controller_is_followed_for_any_jobs(tmp_path, jobs) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.js").write_text(
        "angular.module('m').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: 'ACtrl' });\n"
        "});\n"
    )
    (repo / "index.js").write_text(
        "import ACtrl from './a.controller';\nangular.module('m').controller('ACtrl', ACtrl);\n"
    )
    (repo / "a.controller.js").write_text("\nexport default class ACtrl {}\n")
    for i in range(3):
        (repo / f"c{i}.js").write_text(f"export const c{i} = {i};\n")
    sink = MemorySink()
    pipeline.run(repo, Settings(jobs=jobs), sink)
    [route] = [s for r in sink.records for s in r.statements if s.semanticType == "route"]
    assert (route.handler, route.handlerPath, route.handlerLine) == ("ACtrl", "a.controller.js", 2)


def test_fact_values_survive_pickling_and_resolve_to_the_same_fact(tmp_path) -> None:
    import pickle

    from breezeai_cog.parsers.treesitter import parse_source
    from breezeai_cog.parsers.typescript_angularjs import registrations, states

    src = (
        b"import ACtrl from './a';\n"
        b"angular.module('m').controller('ACtrl', ACtrl).controller('B', function () {});\n"
        b"$stateProvider.state('app.users', { url: '/users' });\n"
    )
    root = parse_source("javascript", src, 0).root_node
    regs = registrations._collect(root, src, "x.js", tmp_path)
    sts = states._collect(root, src, "x.js", tmp_path)
    assert regs is not None and sts is not None
    assert pickle.loads(pickle.dumps(regs)) == regs
    assert pickle.loads(pickle.dumps(sts)) == sts
    # the same values in either order resolve to the same fact
    other = [(("controller", "B"), ("y.js", 7))]
    assert registrations._resolve([regs, other], None) == registrations._resolve(
        [other, regs], None
    )
    # `B` is now registered in two places: neither is believed
    assert ("controller", "B") not in registrations._resolve([regs, other], None)
