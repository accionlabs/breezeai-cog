"""Flask detector: gates, look-alikes that must not be routes, framework label, cross-file wiring
for any ``--jobs``, and schema validity."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.schemas import FileRecord

APP = (
    "from flask import Flask\n"
    "app = Flask(__name__)\n\n"
    "@app.route('/users/<int:id>', methods=['GET', 'PUT'])\n"
    "def user(id):\n    return ''\n"
)


def _parse(tmp_path: Path, source: str, *, path: str = "app.py", capture: bool = True) -> FileRecord:
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source)
    registry.discover_builtin()
    parser = registry.select(path, source.encode())
    assert parser is not None
    ctx = ParseContext(path=path, abs_path=p, source=source.encode(), repo_root=tmp_path,
                       capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.semanticType == "route"]


def test_routes_link_to_the_handler_function(tmp_path) -> None:
    rec = _parse(tmp_path, APP)
    routes = _routes(rec)
    assert sorted(r.method for r in routes) == ["GET", "PUT"]
    assert {r.parentId for r in routes} == {"app.py#user@5"}
    assert {(r.nodeType, r.framework, r.endpoint) for r in routes} == {
        ("synthetic", "flask", "/users/<int:id>")}
    assert rec.framework == "flask"


def test_no_routes_without_statement_capture(tmp_path) -> None:
    rec = _parse(tmp_path, APP, capture=False)
    assert _routes(rec) == []


def test_fixture_files_have_no_routes(tmp_path) -> None:
    rec = _parse(tmp_path, APP, path="app.test.py")
    assert _routes(rec) == []


def test_a_file_importing_flask_is_labelled(tmp_path) -> None:
    rec = _parse(tmp_path, "from flask import render_template\n\ndef page():\n    return render_template('x')\n")
    assert rec.framework == "flask" and _routes(rec) == []


def test_fastapi_decorators_are_not_flask_routes(tmp_path) -> None:
    rec = _parse(tmp_path, (
        "from fastapi import FastAPI\nfrom flask import Flask\napp = FastAPI()\n"
        "@app.get('/items')\ndef items():\n    return []\n"
    ))
    assert {(r.framework, r.endpoint) for r in _routes(rec)} == {("fastapi", "/items")}


def test_look_alikes_are_not_routes(tmp_path) -> None:
    rec = _parse(tmp_path, (
        "from cache import cache\nrouter = make_router()\n"
        "@cache.get('/x')\ndef cached():\n    pass\n"        # not a Flask object, not a Flask file
        "@router.route('/y')\ndef y():\n    pass\n"
    ))
    assert _routes(rec) == [] and rec.framework is None


def test_a_rule_without_a_leading_slash_is_not_a_route(tmp_path) -> None:
    rec = _parse(tmp_path, (
        "from flask import Flask\napp = Flask(__name__)\nconfig = Settings()\n"
        "@config.get('DEBUG')\ndef debug():\n    pass\n"
    ))
    assert _routes(rec) == []


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, APP)
    schema = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    assert not list(schema.iter_errors(json.loads(to_line(rec))))


@pytest.mark.parametrize("jobs", [1, 2])
def test_blueprint_registered_in_another_file_for_any_jobs(tmp_path, jobs) -> None:
    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    files = {
        "app/__init__.py": (
            "from flask import Flask\n"
            "def create_app():\n    app = Flask(__name__)\n"
            "    from app.main import bp as main_bp\n"
            "    app.register_blueprint(main_bp, url_prefix='/main')\n"
            "    return app\n"
        ),
        "app/main/__init__.py": "from flask import Blueprint\nbp = Blueprint('main', __name__)\n",
        "app/main/routes.py": "from app.main import bp\n\n@bp.route('/index')\ndef index():\n    return ''\n",
    }
    for rel, src in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"app/main/routes.py"' in line)
    (route,) = [s for s in rec["statements"] if s.get("semanticType") == "route"]
    assert (route["method"], route["endpoint"], route["handler"]) == ("GET", "/main/index", "index")
    assert rec["framework"] == "flask"  # a route file that never imports Flask


CONDUIT = {
    "conduit/__init__.py": "",
    "conduit/user/__init__.py": "from . import views\n",
    "conduit/user/views.py": (
        "from flask import Blueprint\nblueprint = Blueprint('user', __name__)\n\n"
        "@blueprint.route('/api/users', methods=['POST'])\ndef register():\n    return ''\n"),
    "conduit/profile/__init__.py": "",
    "conduit/profile/views.py": (
        "from flask import Blueprint\nblueprint = Blueprint('profile', __name__)\n\n"
        "@blueprint.route('/<name>')\ndef profile(name):\n    return ''\n"),
    "conduit/other/__init__.py": "",
    "conduit/other/views.py": (
        "from flask import Blueprint\nblueprint = Blueprint('other', __name__)\n\n"
        "@blueprint.route('/x')\ndef x():\n    return ''\n"),
    "conduit/app.py": (
        "from flask import Flask\nfrom conduit import user\nimport conduit.profile.views\n"
        "import thirdparty\n\n"
        "def create_app():\n    app = Flask(__name__)\n"
        "    app.register_blueprint(user.views.blueprint)\n"
        "    app.register_blueprint(conduit.profile.views.blueprint, url_prefix='/profiles')\n"
        "    app.register_blueprint(thirdparty.views.blueprint, url_prefix='/lib')\n"
        "    return app\n"),
}


@pytest.mark.parametrize("jobs", [1, 2])
def test_blueprint_reached_through_a_dotted_chain_for_any_jobs(capture_routes, jobs) -> None:
    routes = capture_routes(CONDUIT, jobs)
    assert [(r["method"], r["endpoint"]) for r in routes["conduit/user/views.py"]] == [("POST", "/api/users")]
    assert [(r["method"], r["endpoint"]) for r in routes["conduit/profile/views.py"]] == \
        [("GET", "/profiles/<name>")]
    # registered nowhere the code shows (the chain `thirdparty.views` is not a module of the repo)
    assert [r.get("endpoint") for r in routes["conduit/other/views.py"]] == [None]
