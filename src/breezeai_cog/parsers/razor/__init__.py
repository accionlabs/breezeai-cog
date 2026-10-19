"""Razor (``.cshtml`` / ``.razor``) template language parser package."""

from __future__ import annotations

from .parser import RazorParser

PARSERS = [RazorParser()]
