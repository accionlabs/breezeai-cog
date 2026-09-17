from breezeai_cog.parsers.typescript_fastify.routes import detect_fastify_routes
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.schemas import FileRecord


def _make_file_record() -> FileRecord:
    return FileRecord(
        id="app.ts",
        path="app.ts",
        type="code",
        language="typescript",
        loc=0,
        functions=[],
        statements=[],
    )


def test_detects_route_from_known_fastify_instance():
    source = b"""
    import Fastify from "fastify";

    const fastify = Fastify();

    fastify.get("/users", async (request, reply) => {
        return { users: [] };
    });
    """

    result = parse_source("typescript", source, 1_000_000)

    routes = detect_fastify_routes(
        result.root_node,
        source,
        "app.ts",
        record=_make_file_record(),
        seen_ids=set(),
    )

    assert len(routes) == 1

    route = routes[0]

    print("\nROUTE:", route)
    print("\nROUTE DICT:", route.model_dump())

    assert route.framework == "fastify"
    assert route.method == "GET"


def test_ignores_unrelated_get_method():
    source = b"""
    import axios from "axios";

    const client = axios;

    client.get("/users", async () => {
        return [];
    });
    """

    result = parse_source("typescript", source, 1_000_000)

    routes = detect_fastify_routes(
        result.root_node,
        source,
        "app.ts",
        record=_make_file_record(),
        seen_ids=set(),
    )

    assert routes == []

def test_detects_post_route():
    source = b"""
    import Fastify from "fastify";

    const fastify = Fastify();

    fastify.post("/users", async (request, reply) => {
        return { created: true };
    });
    """

    result = parse_source("typescript", source, 1_000_000)

    routes = detect_fastify_routes(
        result.root_node,
        source,
        "app.ts",
        record=_make_file_record(),
        seen_ids=set(),
    )

    assert len(routes) == 1
    assert routes[0].framework == "fastify"
    assert routes[0].method == "POST"

def test_detects_route_with_named_handler():
    source = b"""
    import Fastify from "fastify";

    const fastify = Fastify();

    function getUsers(request, reply) {
        return { users: [] };
    }

    fastify.get("/users", getUsers);
    """

    result = parse_source("typescript", source, 1_000_000)

    routes = detect_fastify_routes(
        result.root_node,
        source,
        "app.ts",
        record=_make_file_record(),
        seen_ids=set(),
    )

    assert len(routes) == 1
    assert routes[0].framework == "fastify"
    assert routes[0].method == "GET"