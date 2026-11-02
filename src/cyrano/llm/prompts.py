"""Texts the client adds to a request when it asks for JSON."""

from __future__ import annotations

SCHEMA_INSTRUCTION = (
    "Answer with a single JSON object and nothing else: no prose, no explanations, "
    "no markdown. The JSON object must be valid against this JSON Schema:\n{schema}"
)
INVALID_REPLY_INSTRUCTION = (
    "Your previous reply is not valid. Fix these problems and answer again with a single "
    "JSON object only:\n{problems}"
)
TRUNCATED_REPLY_INSTRUCTION = (
    "Your previous reply was cut off because it was too long. Make the JSON output shorter."
)
PROBLEM_LINE = "- {path}: {message}"
