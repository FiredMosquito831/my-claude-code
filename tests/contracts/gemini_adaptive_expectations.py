"""Finite, recorded Gemini ADAPTIVE ``include_thoughts`` moves for 8.46.0 (PR 8).

The original ``marketplace_equality_baseline.json`` (v7.108.0) and
``adaptive_every_door_expectations.json`` (8.13.0) remain unchanged. These
expectations record only the exact 68 ``chat/gemini/*/adaptive/*`` cells moved
by G-Q2 (emitting ``extra_body.google.thinking_config.include_thoughts = True``
when ``GeminiProvider`` handles ``ReasoningControl.ADAPTIVE``). Every other cell
across all 53 Chat hosts, ``messages/*``, and ``responses/*`` remains 100%
byte-identical.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("gemini_adaptive_expectations.json")


def gemini_adaptive_corrections() -> dict[str, dict[str, str]]:
    """Return ``gemini/<corpus-key> -> {before, adaptive_before, after}``."""

    document: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, str]] = document["cases"]["market"]
    return entries
