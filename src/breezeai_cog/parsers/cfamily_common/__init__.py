"""The extraction, index and include handling C, C++ and CUDA share (``parsers/c``,
``parsers/cpp``, ``parsers/cuda``).

Not a parser: it registers nothing. Each language subclasses :class:`.parser.CFamilyParser`
with its own grammar, extensions, index and evidence.
"""
