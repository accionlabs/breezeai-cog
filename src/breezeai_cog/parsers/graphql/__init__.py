"""Standalone GraphQL (``.graphql`` / ``.gql``) language parser package."""

from __future__ import annotations

from .parser import GraphQLParser

PARSERS = [GraphQLParser()]
