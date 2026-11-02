"""The extraction, index and include handling C and C++ share (``parsers/c``, ``parsers/cpp``).

Not a parser: it registers nothing. Each language subclasses :class:`.parser.CFamilyParser`
with its own grammar, extensions, index and evidence.
"""
