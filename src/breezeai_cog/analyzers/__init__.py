"""Non-AST analyzers (SQL DDL, Elasticsearch mappings) → DB-ontology NDJSON records.

These emit framework-specific record shapes (``__type: "ddl" | "es_index" |
"es_settings"``) that go straight to S3/NDJSON — they are not capture-schema FileRecords.
"""
