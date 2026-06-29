"""Language parsers. Each ``parsers/<lang>/`` subpackage self-registers via
``core.registry.register`` on import (discovered by ``registry.discover_builtin``).
``base`` (the plugin contract), ``treesitter`` (shared grammar helpers) and
``detection`` (shared cross-language detectors) are not parsers themselves.
"""
