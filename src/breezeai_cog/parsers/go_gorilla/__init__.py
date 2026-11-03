"""gorilla/mux route detection for Go. A registered additive detector (``routes.py``) — it owns no file,
so it runs on every Go file that imports the package, whatever else the file does; the router
analysis it shares with the other Go routers is ``parsers/go/routing.py``."""

PARSERS: list = []
