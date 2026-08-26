"""Standalone Prisma Schema Language (``.prisma``) language parser package."""

from __future__ import annotations

from .parser import PrismaParser

PARSERS = [PrismaParser()]
