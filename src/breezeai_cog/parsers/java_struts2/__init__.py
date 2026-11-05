"""Struts2: ``struts.xml`` actions (a selecting parser for Struts2 configuration files) and
Convention-plugin annotations (an additive Java detector, :mod:`.annotations`, so an action
class that is also a Spring bean keeps its Spring records)."""

from __future__ import annotations

from .config import Struts2ConfigParser

PARSERS = [Struts2ConfigParser()]
