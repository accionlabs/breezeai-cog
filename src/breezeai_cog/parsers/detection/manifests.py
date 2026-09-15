"""Repo-level ORM evidence from build manifests (Layer 3 of the datastore gate).

Layer 2 (:func:`..db_queries.vendor_from_imports`) resolves the product when the calling
file imports it. It cannot resolve the *dependency-injected* case, which is the norm in
Spring and ASP.NET: the service imports only an internal ``OrderRepository`` interface,
while the ORM dependency lives in ``pom.xml`` / ``build.gradle`` / ``*.csproj``. This
module supplies that repo-wide fallback.

Deliberately conservative in three ways, because repo-level evidence is weaker than a
file's own imports:

* **Only ORM-family products.** The value replaces the generic ``orm`` hint, so only
  ORM/relational products are candidates. Document stores and caches (mongodb, redis,
  elasticsearch…) are excluded: their calls carry distinctive verbs that already resolve
  earlier, and a repo-level guess would just as easily be wrong.
* **Only when unambiguous.** A repo declaring two different ORMs yields ``None`` (stay
  generic) rather than picking one — mirroring the unanimity rule the HCL parser uses for
  ``File.platform``. The one collapse is ``{hibernate, jpa}``, which is a single stack:
  Spring Data JPA's default provider *is* Hibernate.
* **Bounded search.** Root and one directory level only — no recursive walk, so a large
  monorepo cannot make this expensive. Covers the ordinary single-project and
  ``<root>/<module>/pom.xml`` layouts.

Matching is a substring test over the raw manifest text rather than a real POM/Gradle/npm
parse: the markers are distinctive coordinates, the result is only a fallback, and any
ambiguity degrades to ``None`` — so a stray match costs precision, never correctness of a
stronger signal.
"""

from __future__ import annotations

from pathlib import Path

#: Dependency-coordinate substring -> ORM-family hint. Searched case-insensitively in the
#: manifest text. Ordered for readability only; every match is collected before deciding.
_ORM_BY_DEPENDENCY: tuple[tuple[str, str], ...] = (
    # JVM
    ("hibernate-core", "hibernate"),
    ("org.hibernate", "hibernate"),
    ("spring-boot-starter-data-jpa", "jpa"),
    ("jakarta.persistence", "jpa"),
    ("javax.persistence", "jpa"),
    ("eclipselink", "jpa"),
    # .NET  (NHibernate before the EF markers: it is not an EF package)
    ("nhibernate", "hibernate"),
    ("microsoft.entityframeworkcore", "entity_framework"),
    ("entityframework", "entity_framework"),
    # Node
    ("@prisma/client", "prisma"),
    ("typeorm", "typeorm"),
    ("sequelize", "sequelize"),
    # Python
    ("sqlalchemy", "sqlalchemy"),
    ("django", "django"),
)

#: Hibernate and JPA are one stack, not two competing ORMs — a pom naming both the
#: Spring Data JPA starter and hibernate-core is still a Hibernate project.
_JPA_FAMILY = frozenset({"hibernate", "jpa"})

#: ORMs the ``dataAccessHint`` vocabulary has no value for. They cannot be *named*, but they
#: must still count toward ambiguity: without this, a repo declaring both Hibernate and
#: MyBatis would look unanimous and every ambiguous call would be labelled ``hibernate``.
#: Contributing an unnameable sentinel makes the guard honest — the repo has two ORMs, so no
#: fallback is offered. Give one of these a vocabulary value and it moves to the table above.
_UNNAMEABLE_ORM_MARKERS: tuple[str, ...] = (
    "mybatis", "org.jooq", "jooq-", "dapper", "peewee", "tortoise-orm",
    "pony", "activerecord", "ormlite", "objectbox", "jdbi",
)

#: Stands in for "an ORM we cannot name" in the collected set (see above). Never returned.
_UNNAMEABLE = "\x00unnameable"

#: Manifest globs, relative to the repo root. Root plus one level (monorepo modules);
#: never a recursive walk.
_MANIFEST_GLOBS: tuple[str, ...] = (
    "pom.xml", "*/pom.xml",
    "build.gradle", "*/build.gradle",
    "build.gradle.kts", "*/build.gradle.kts",
    "gradle/libs.versions.toml",
    "package.json", "*/package.json",
    "requirements.txt", "*/requirements.txt",
    "pyproject.toml", "*/pyproject.toml",
    "Pipfile", "*/Pipfile",
    "*.csproj", "*/*.csproj",
    "*.vbproj", "*/*.vbproj",
    "packages.config", "*/packages.config",
)

#: Cap on manifests read, so a repo with hundreds of modules stays cheap. Ambiguity
#: already short-circuits the answer, so reading more rarely changes the outcome.
_MAX_MANIFESTS = 40

#: Per-manifest read cap — a dependency list is small; anything larger is not a manifest.
_MAX_BYTES = 512_000


def _manifest_paths(repo_root: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in _MANIFEST_GLOBS:
        for p in repo_root.glob(pattern):
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            out.append(p)
            if len(out) >= _MAX_MANIFESTS:
                return out
    return out


def scan_repo_orm(repo_root: Path) -> str | None:
    """The single ORM-family product this repo's build manifests declare, else ``None``.

    ``None`` means "not establishable" — no ORM declared, or more than one — and leaves the
    generic ``orm`` hint in place. Never raises: an unreadable manifest is skipped.
    """
    found: set[str] = set()
    for path in _manifest_paths(repo_root):
        try:
            text = path.read_bytes()[:_MAX_BYTES].decode("utf-8", "replace").lower()
        except OSError:
            continue
        for needle, hint in _ORM_BY_DEPENDENCY:
            if needle in text:
                found.add(hint)
        if any(marker in text for marker in _UNNAMEABLE_ORM_MARKERS):
            found.add(_UNNAMEABLE)
    if not found:
        return None
    if found == _JPA_FAMILY:
        return "hibernate"
    if len(found) == 1:
        # An unnameable ORM on its own is still "some ORM" — the generic hint is already
        # that, so there is nothing to add.
        return None if _UNNAMEABLE in found else next(iter(found))
    return None  # two or more distinct ORMs — stay honest, keep the generic hint
