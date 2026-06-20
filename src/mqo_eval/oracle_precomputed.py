"""Precomputed gold oracle for mqo-eval.

Reads a pre-minted gold cache JSON (produced by scripts/mint-gold.py) so eval
runs don't need a live PGWire connection for scoring.

Cache format (corpus/gold_<server>.json):
    {
      "<case_id>": {"columns": [...], "rows": [[...], ...]},
      ...
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .oracle_pgwire import OracleError, ReferenceTable, ReferenceResult


@dataclass
class PrecomputedConfig:
    """Points to a pre-minted gold cache file."""

    gold_file: Path

    def load(self) -> dict[str, ReferenceTable | OracleError]:
        """Load the cache, returning a map of case_id → ReferenceTable|OracleError."""
        raw = json.loads(self.gold_file.read_text())
        out: dict[str, ReferenceTable | OracleError] = {}
        for case_id, entry in raw.items():
            if "error" in entry:
                out[case_id] = OracleError(case_id=case_id, message=entry["error"])
            else:
                out[case_id] = ReferenceTable(
                    columns=entry["columns"],
                    rows=entry["rows"],
                )
        return out


def execute_golden_precomputed(
    cache: dict[str, ReferenceTable | OracleError],
    case_id: str,
) -> ReferenceResult:
    """Return the pre-minted gold for *case_id*, or an OracleError if missing."""
    result = cache.get(case_id)
    if result is None:
        return OracleError(case_id=case_id, message="case not in gold cache")
    return result
