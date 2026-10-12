"""Extraction variants evaluated by run_eval.py.

`current` runs the prompt, schema and acceptance rules exactly as they are in
watchdog.py. Other variants monkeypatch `watchdog.SYSTEM_PROMPT` (and, if
needed, `watchdog.extraction_schema`) without editing watchdog.py, so a new
prompt idea can be measured against `current` before it is adopted.
"""
from __future__ import annotations

from typing import Any, Callable

import watchdog

CURRENT_PROMPT = watchdog.SYSTEM_PROMPT
CURRENT_SCHEMA = watchdog.extraction_schema

# The prompt shipped up to 0.4.0, kept so its results can be reproduced.
LEGACY_PROMPT = """You are a conservative engineering-history extractor.
Record only case-study-worthy events supported by supplied evidence: meaningful
technical/product decisions, discoveries, failed approaches, constraints,
milestones, measurable results, or reusable lessons. Ignore routine edits,
formatting, renames, dependency churn, ordinary bug fixes, and unsupported
inferences. Never invent a motivation, alternative, result, metric, or file.
Use concise factual English. When the evidence is insufficient, return
worth_recording=false and an empty entries array. A single input may yield several
distinct entries, but do not split one idea artificially."""

VARIANTS: dict[str, dict[str, Any]] = {
    "current": {"system_prompt": CURRENT_PROMPT, "schema": None, "post_filter": None},
    "legacy": {"system_prompt": LEGACY_PROMPT, "schema": None, "post_filter": None},
}


def apply(name: str) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    variant = VARIANTS[name]
    watchdog.SYSTEM_PROMPT = variant["system_prompt"]
    watchdog.extraction_schema = variant.get("schema") or CURRENT_SCHEMA
    return variant.get("post_filter")


def reset() -> None:
    watchdog.SYSTEM_PROMPT = CURRENT_PROMPT
    watchdog.extraction_schema = CURRENT_SCHEMA
