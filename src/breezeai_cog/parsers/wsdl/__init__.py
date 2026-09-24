"""Standalone WSDL 1.1 (SOAP service contract) language parser package."""

from __future__ import annotations

from .parser import WsdlParser

PARSERS = [WsdlParser()]
