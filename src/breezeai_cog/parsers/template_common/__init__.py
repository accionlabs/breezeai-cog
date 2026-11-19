"""What the template parsers share (HTML, Angular, Razor, Web Forms, JSP, PHP pages, Twig):
reading a page's inline ``<script>`` blocks as one program (``scripts``), over the script
blocks of the page and their true offsets (``script_blocks``). Each parser keeps reading its
own markup and server code.

Not a parser: it registers nothing.
"""
