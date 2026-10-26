"""gRPC service detection for Go. A registered additive detector (``routes.py``) with a fact
(``services.py``) that reads the generated ``*_grpc.pb.go`` files — it owns no file."""

PARSERS: list = []
