"""Standalone Prisma Schema Language (PSL) capture.

The ``prisma`` language parser owns ``.prisma`` files. It emits one flat ``Statement`` per
top-level block — a ``model`` as a ``data_model`` entity (full body, including fields,
``@relation`` and ``@@`` block attributes, on ``text``), an ``enum`` and the
``datasource``/``generator`` config blocks as plain statements — each carrying its declared
``name``. Capture is gated on ``--capture-statements``.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.prisma.parser import PrismaParser
from breezeai_cog.schemas import FileRecord

SCHEMA = """\
// datasource config
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

enum Role {
  USER
  ADMIN
}

/// A registered user
model User {
  id        Int      @id @default(autoincrement())
  email     String   @unique
  role      Role     @default(USER)
  posts     Post[]
  createdAt DateTime @default(now())

  @@map("users")
  @@index([email])
}

model Post {
  id       Int    @id @default(autoincrement())
  title    String
  author   User   @relation(fields: [authorId], references: [id])
  authorId Int
}
"""


def _parse(tmp_path, filename: str, src: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=1000,
    )
    return PrismaParser().parse_file(ctx)


def _by_name(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = PrismaParser()
    assert p.name == "prisma"
    assert p.extensions == (".prisma",)
    assert "prisma" in p.frameworks


def test_model_is_data_model_entity_with_full_body(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    assert rec.language == "prisma"
    assert rec.framework == "prisma"
    user = _by_name(rec, "User", "model_block")
    assert user.semanticType == "data_model"
    assert user.endpoint == "User"
    assert user.parentId == rec.id
    # full body carries fields, relations, and block attributes as text (contents capture)
    assert "email     String" in user.text
    assert "posts     Post[]" in user.text
    assert '@@map("users")' in user.text


def test_relation_carried_on_text_not_as_edge(tmp_path) -> None:
    # A model→model relation is honest-null: it stays visible in the referencing model's text,
    # but the parser never fabricates a statement→statement edge from it.
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    post = _by_name(rec, "Post", "model_block")
    assert "@relation(fields: [authorId], references: [id])" in post.text
    # endpoint is the model's own name (the join key a reference resolves to), not a target.
    assert post.endpoint == "Post"


def test_enum_is_plain_statement_with_members_on_text(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    role = _by_name(rec, "Role", "enum_block")
    assert role.semanticType is None  # enums carry no entity marker (members live on text)
    assert role.endpoint == "Role"
    assert "USER" in role.text and "ADMIN" in role.text


def test_config_blocks_are_plain_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    ds = _by_name(rec, "db", "key_value_block")
    assert ds.semanticType is None
    assert "postgresql" in ds.text
    gen = _by_name(rec, "client", "key_value_block")
    assert gen.semanticType is None
    assert "prisma-client-js" in gen.text


def test_doc_comment_captured(tmp_path) -> None:
    # `/// A registered user` precedes the User model (outside its span) → its own comment stmt.
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    docs = [s for s in rec.statements if s.nodeType == "document_comment"]
    assert any("A registered user" in s.text for s in docs)


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.prisma", SCHEMA, capture=False)
    assert rec.statements == []
    assert rec.language == "prisma"
    assert rec.framework is None


def test_unsupported_and_empty_blocks_are_skipped_not_guessed(tmp_path) -> None:
    # v13 grammar gap: `type` composite blocks and empty model/enum blocks parse to ERROR nodes.
    # Absent beats wrong — the surrounding valid model is still captured, the rest is skipped.
    src = (
        "model Keep {\n  id Int @id\n}\n\ntype Address {\n  street String\n}\n\nmodel Empty {\n}\n"
    )
    rec = _parse(tmp_path, "schema.prisma", src)
    names = {s.name for s in rec.statements if s.nodeType == "model_block"}
    assert "Keep" in names
    assert not any(s.name == "Address" for s in rec.statements)


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.prisma", SCHEMA)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.statements  # non-empty


def test_registered_and_selected_by_extension() -> None:
    registry.clear()
    registry.discover_builtin()
    sel = registry.select("schema.prisma", SCHEMA.encode())
    assert sel is not None and sel.name == "prisma"
    assert ".prisma" in registry.capabilities()["extensions"]
    assert "prisma" in registry.capabilities()["languages"]
    registry.clear()
