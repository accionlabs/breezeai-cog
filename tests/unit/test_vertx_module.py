"""A Vert.x 2 ``mod.json`` records the verticle its ``"main"`` names as a ``verticle_deploy``
statement, led to that verticle's own ``start()`` when it is a repo class that declares one."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from breezeai_cog import analyze_repo

RES = "src/main/resources"
JAVA = "src/main/java/com/acme"

_FILES = {
    # a repo class declaring start()
    f"orders/{RES}/mod.json": '{ "main": "com.acme.orders.OrdersVerticle", "worker": false }\n',
    f"orders/{JAVA}/orders/OrdersVerticle.java": (
        "package com.acme.orders;\n"
        "import org.vertx.java.platform.Verticle;\n"
        "public class OrdersVerticle extends Verticle {\n"
        "  public void start() {\n"
        '    vertx.eventBus().registerHandler("orders.create", null);\n'
        "  }\n"
        "}\n"),
    # a repo class whose start() is inherited; "main" on line 3, a JSONC comment
    f"inherit/{RES}/mod.json": '{\n  // the module\n  "main": "com.acme.Child"\n}\n',
    f"inherit/{JAVA}/Child.java": "package com.acme;\npublic class Child extends Base { }\n",
    f"inherit/{JAVA}/Base.java": (
        "package com.acme;\n"
        "public class Base extends org.vertx.java.platform.Verticle { public void start() {} }\n"),
    # a class that is not in the repository (a module key says it is a Vert.x descriptor)
    f"ext/{RES}/mod.json": '{"main": "io.vertx.Missing", "multi-threaded": false}',
    # a script verticle next to mod.json, and one that is not in the repository
    "js/mod.json": '{"main": "app.js", "auto-redeploy": true}',
    "js/app.js": "vertx.eventBus();\n",
    "rb/mod.json": '{"main": "jruby:app.rb"}',
    # no sign of Vert.x: a mod.json of another tool, with a "main" of its own
    "npm/mod.json": '{"name": "x", "main": "index.js"}',
    "npm/index.js": "module.exports = 1;\n",
    f"nocls/{RES}/mod.json": '{"main": "com.acme.Nowhere"}',
    # a Groovy class, through the language prefix
    f"gmod/{RES}/mod.json": '{"main": "groovy:com.acme.GV"}',
    "gmod/src/main/groovy/com/acme/GV.groovy": (
        "package com.acme\n"
        "class GV extends org.vertx.groovy.platform.Verticle {\n"
        "  def start() { }\n"
        "}\n"),
    # a Java class deployed through the Groovy factory: no Groovy class has the name
    f"gjava/{RES}/mod.json": '{"main": "groovy:com.acme.JV"}',
    f"gjava/{JAVA}/JV.java": (
        "package com.acme;\n"
        "public class JV extends org.vertx.java.platform.Verticle {\n"
        "  public void start() {}\n"
        "}\n"),
    # a Groovy class that inherits start() is not passed over for a Java class of its name
    f"gshadow/{RES}/mod.json": '{"main": "groovy:com.acme.Dup"}',
    "gshadow/src/main/groovy/com/acme/Dup.groovy": (
        "package com.acme\n"
        "class Dup extends org.vertx.groovy.platform.Verticle { }\n"),
    f"gshadow/{JAVA}/Dup.java": (
        "package com.acme;\n"
        "public class Dup extends org.vertx.java.platform.Verticle {\n"
        "  public void start() {}\n"
        "}\n"),
    # two start() declarations: which one Vert.x calls depends on its version
    f"over/{RES}/mod.json": '{"main": "com.acme.Over"}',
    f"over/{JAVA}/Over.java": (
        "package com.acme;\n"
        "public class Over extends org.vertx.java.platform.Verticle {\n"
        "  public void start() {}\n"
        "  public void start(org.vertx.java.core.Future<Void> f) {}\n"
        "}\n"),
    # a library module, and a mod.json that is no Vert.x descriptor
    "lib/mod.json": '{"includes": "io.vertx~lang-groovy~2.0"}',
    "other/mod.json": '{"name": "x", "version": 1}',
}


def _deploys(tmp_path: Path, jobs: int) -> dict[str, tuple]:
    repo = tmp_path / "repo"
    for rel, src in _FILES.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, out=tmp_path / f"out{jobs}", jobs=jobs)
    records = [json.loads(line) for line in
               gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()[1:]]
    out: dict[str, tuple] = {}
    for r in records:
        if r.get("path", "").endswith("mod.json"):
            stmts = [s for s in r.get("statements", []) if s.get("semanticType")]
            out[r["path"].split("/", 1)[0]] = tuple(
                (s["semanticType"], s["nodeType"], s["framework"], s["startLine"], s.get("endpoint"),
                 s.get("handler"), s.get("handlerPath"), s.get("handlerLine")) for s in stmts)
    return out


@pytest.mark.parametrize("jobs", [1, 2])
def test_mod_json_main_is_a_verticle_deploy(tmp_path: Path, jobs: int) -> None:
    got = _deploys(tmp_path, jobs)
    d = ("verticle_deploy", "synthetic", "vertx")
    assert got == {
        "orders": (d + (1, "com.acme.orders.OrdersVerticle", "start",
                        f"orders/{JAVA}/orders/OrdersVerticle.java", 4),),
        "inherit": (d + (3, "com.acme.Child", None, None, None),),
        "ext": (d + (1, "io.vertx.Missing", None, None, None),),
        "js": (d + (1, "app.js", None, "js/app.js", None),),
        "rb": (d + (1, "app.rb", None, None, None),),
        "gmod": (d + (1, "com.acme.GV", "start", "gmod/src/main/groovy/com/acme/GV.groovy", 3),),
        "gjava": (d + (1, "com.acme.JV", "start", f"gjava/{JAVA}/JV.java", 3),),
        "gshadow": (d + (1, "com.acme.Dup", None, None, None),),
        "over": (d + (1, "com.acme.Over", None, None, None),),
        "lib": (),
        "other": (),
        "npm": (),
        "nocls": (),
    }


def test_no_statement_without_statement_capture(tmp_path: Path) -> None:
    from breezeai_cog.parsers.base import ParseContext
    from breezeai_cog.parsers.structured_json.parser import JsonParser

    p = tmp_path / "mod.json"
    p.write_text('{"main": "com.acme.App"}')
    ctx = ParseContext(path="mod.json", abs_path=p, source=p.read_bytes(), repo_root=tmp_path,
                       capture_statements=False)
    rec = JsonParser().parse_file(ctx)
    assert rec.statements == [] and rec.metadata["verticleMain"] == "com.acme.App"
