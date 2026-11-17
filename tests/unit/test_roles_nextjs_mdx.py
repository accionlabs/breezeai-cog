"""Next.js MDX pages (docs/entry-point-roles/nextjs.md, "MDX pages"): one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

PKG = '{"dependencies": {"next": "14", "react": "18", "@next/mdx": "14"}}'
PKG_16 = '{"dependencies": {"next": "^16.0.0", "react": "19", "@next/mdx": "16"}}'
MDX_CONFIG = "const nextConfig = { pageExtensions: ['tsx', 'mdx'] };\nexport default nextConfig;\n"
PAGE = "# About us\n\nWe make charts.\n"


def _pages(stmts):
    return [s for s in stmts if is_entry_point(s) and s.routeKind == "page"]


def _run(analyze_files, files: dict[str, str], pkg: str = PKG):
    return analyze_files({"package.json": pkg, **files})


def test_app_router_mdx_page(analyze_files) -> None:
    """``pageExtensions`` lists ``mdx``: ``app/about/page.mdx`` is the ``/about`` page, with no
    handler, a synthetic node and the file's first line as its text."""
    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "app/about/page.mdx": PAGE})
    [p] = _pages(st)
    assert (p.semanticType, p.routeKind, p.framework, p.endpoint, p.handler) == (
        "route",
        "page",
        "nextjs",
        "/about",
        None,
    )
    assert (p.nodeType, p.text, p.startLine, p.endLine) == ("synthetic", "# About us", 1, 1)
    assert p.id == "app/about/page.mdx:1:0"
    assert p.parentId == "app/about/page.mdx"
    assert p.authRequired is None and p.guards is None


def test_mdx_page_without_handler_is_not_a_contract_gap(analyze_files) -> None:
    from breezeai_cog.parsers.contract import _check

    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "app/about/page.mdx": PAGE})
    [p] = _pages(st)
    assert _check(p) == []


def test_pages_router_mdx_page_with_base_path(analyze_files) -> None:
    """``basePath`` prefixes a Pages Router MDX page's URL; an app whose pages are all MDX is
    still a Next.js app."""
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default { basePath: '/docs', pageExtensions: ['tsx', 'mdx'] };\n",
            "pages/blog/post.mdx": PAGE,
        },
    )
    [p] = _pages(st)
    assert p.endpoint == "/docs/blog/post"


def test_mdx_page_covered_by_proxy(analyze_files) -> None:
    """A ``proxy.ts`` matcher covering the page adds the ``proxy`` guard; it does not set
    ``authRequired``."""
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "proxy.ts": "export function proxy(req) {\n  return undefined;\n}\n"
            "export const config = { matcher: ['/about/:path*'] };\n",
            "app/about/page.mdx": PAGE,
        },
        pkg=PKG_16,
    )
    [p] = _pages(st)
    assert p.guards == ["proxy"]
    assert p.authRequired is None


def test_mdx_page_covered_by_middleware(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "middleware.ts": "export function middleware(req) {}\n",
            "app/about/page.mdx": PAGE,
        },
    )
    [p] = _pages(st)
    assert p.guards == ["middleware"]


def test_mdx_not_in_page_extensions_is_not_a_page(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default { pageExtensions: ['tsx', 'ts'] };\n",
            "app/about/page.mdx": PAGE,
        },
    )
    assert _pages(st) == []


def test_no_page_extensions_is_not_a_page(analyze_files) -> None:
    """Next.js's default extensions do not include ``.mdx``."""
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default {};\n",
            "app/layout.tsx": "export default function L({ children }) { return children; }\n",
            "app/about/page.mdx": PAGE,
        },
    )
    assert _pages(st) == []


def test_parallel_route_slot_is_not_a_page(analyze_files) -> None:
    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "app/dashboard/@team/page.mdx": PAGE})
    assert _pages(st) == []


def test_private_folder_is_not_a_page(analyze_files) -> None:
    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "app/_drafts/page.mdx": PAGE})
    assert _pages(st) == []


def test_intercepting_route_is_not_a_page(analyze_files) -> None:
    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "app/feed/(.)photo/page.mdx": PAGE})
    assert _pages(st) == []


def test_pages_api_mdx_is_not_a_page(analyze_files) -> None:
    st = _run(analyze_files, {"next.config.mjs": MDX_CONFIG, "pages/api/notes.mdx": PAGE})
    assert _pages(st) == []


def test_mdx_outside_a_next_app_is_a_file_only(analyze_files, tmp_path) -> None:
    from breezeai_cog import iter_file_records

    st = analyze_files({"docs/getting-started.mdx": PAGE})
    assert _pages(st) == []
    [rec] = [r for r in iter_file_records(tmp_path / "repo") if r.path.endswith(".mdx")]
    assert rec.language == "mdx"


def test_docusaurus_pages_without_next_are_not_pages(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"@docusaurus/core": "3"}}',
            "src/pages/index.tsx": "export default function Home() { return null; }\n",
            "src/pages/about.mdx": PAGE,
        }
    )
    assert _pages(st) == []


def test_page_extensions_not_a_literal_is_not_a_page(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "import base from './exts.mjs';\n"
            "export default { pageExtensions: [...base, 'mdx'] };\n",
            "exts.mjs": "export default ['tsx'];\n",
            "app/about/page.mdx": PAGE,
        },
    )
    assert _pages(st) == []


def test_route_group_dynamic_segment_and_index(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "app/(marketing)/blog/[slug]/page.mdx": PAGE,
            "pages/guides/index.mdx": PAGE,
        },
    )
    assert sorted(p.endpoint for p in _pages(st)) == ["/blog/[slug]", "/guides"]


def test_suffixed_page_extension(analyze_files) -> None:
    """``pageExtensions: ['page.mdx']`` — only ``*.page.mdx`` files are pages."""
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default { pageExtensions: ['page.tsx', 'page.mdx'] };\n",
            "pages/docs.page.mdx": PAGE,
            "pages/notes.mdx": PAGE,
        },
    )
    assert [p.endpoint for p in _pages(st)] == ["/docs"]


def test_special_and_story_files_are_not_pages(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "pages/_app.mdx": PAGE,
            "pages/404.mdx": PAGE,
            "pages/button.stories.mdx": PAGE,
        },
    )
    assert _pages(st) == []


def test_unknown_base_path_leaves_the_endpoint_empty(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "const base = process.env.BASE;\n"
            "export default { basePath: base, pageExtensions: ['tsx', 'mdx'] };\n",
            "app/about/page.mdx": PAGE,
        },
    )
    [p] = _pages(st)
    assert p.endpoint is None


def test_mdx_page_beside_tsx_pages(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "app/page.tsx": "export default function Home() { return null; }\n",
            "app/about/page.mdx": "import Head from 'next/head'\n\n" + PAGE,
        },
    )
    assert sorted((p.path, p.endpoint) for p in _pages(st)) == [
        ("app/about/page.mdx", "/about"),
        ("app/page.tsx", "/"),
    ]


def test_malformed_config_keeps_the_mdx_file(analyze_files, tmp_path) -> None:
    from breezeai_cog import iter_file_records

    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default { pageExtensions: [ ;;; \n",
            "app/about/page.mdx": PAGE,
        },
    )
    assert _pages(st) == []
    langs = {r.path: r.language for r in iter_file_records(tmp_path / "repo")}
    assert langs["app/about/page.mdx"] == "mdx"


def test_jobs_one_and_two_agree(capture_routes) -> None:
    files = {
        "package.json": PKG,
        "next.config.mjs": "export default { basePath: '/docs', pageExtensions: ['tsx', 'mdx'] };\n",
        "app/about/page.mdx": PAGE,
        "pages/blog/post.mdx": PAGE,
        "app/page.tsx": "export default function Home() { return null; }\n",
    }
    one, two = capture_routes(files, 1), capture_routes(files, 2)
    assert one == two
    assert one["app/about/page.mdx"][0]["endpoint"] == "/docs/about"
    assert one["pages/blog/post.mdx"][0]["endpoint"] == "/docs/blog/post"


def test_src_app_and_pages_roots(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "src/app/about/page.mdx": PAGE,
            "src/pages/blog/post.mdx": PAGE,
        },
    )
    assert sorted(p.endpoint for p in _pages(st)) == ["/about", "/blog/post"]


def test_nested_app_without_its_own_config_does_not_take_the_parents(analyze_files) -> None:
    """``docs`` is a Next.js app (its ``package.json``) with no ``next.config``: Next.js's
    default applies there, so the root config's ``mdx`` does not make its ``.mdx`` pages."""
    st = _run(
        analyze_files,
        {
            "next.config.mjs": MDX_CONFIG,
            "app/about/page.mdx": PAGE,
            "docs/package.json": PKG,
            "docs/pages/intro.mdx": PAGE,
        },
    )
    assert [(p.parentId, p.endpoint) for p in _pages(st)] == [("app/about/page.mdx", "/about")]


def test_nested_app_with_its_own_config(analyze_files) -> None:
    st = _run(
        analyze_files,
        {
            "next.config.mjs": "export default { pageExtensions: ['tsx'] };\n",
            "app/about/page.mdx": PAGE,
            "docs/package.json": PKG,
            "docs/next.config.mjs": "export default { basePath: '/docs', pageExtensions: ['mdx'] };\n",
            "docs/pages/intro.mdx": PAGE,
        },
    )
    assert [(p.parentId, p.endpoint) for p in _pages(st)] == [
        ("docs/pages/intro.mdx", "/docs/intro")
    ]
