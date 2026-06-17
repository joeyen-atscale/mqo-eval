"""PR #42 corpus loader — tpcds_sql_derived_limited.yaml schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Query:
    id: str
    nl_query: str
    expected_sql: str
    disabled: bool = False
    equivalent_attributes: list[list[str]] = field(default_factory=list)


@dataclass
class Corpus:
    context: str
    queries: list[Query]
    path: Path

    @property
    def active(self) -> list[Query]:
        return [q for q in self.queries if not q.disabled]

    @property
    def skipped(self) -> list[Query]:
        return [q for q in self.queries if q.disabled]


def load_corpus(path: Path | str) -> Corpus:
    """Load a PR#42-style corpus YAML; raises FileNotFoundError or ValueError."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"corpus file not found: {p}")

    raw: Any = yaml.safe_load(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"corpus must be a YAML mapping, got {type(raw).__name__}")

    context: str = raw.get("context") or ""
    raw_queries: list[Any] = raw.get("queries", [])
    if not isinstance(raw_queries, list):
        raise ValueError("corpus 'queries' must be a list")

    queries: list[Query] = []
    for i, item in enumerate(raw_queries):
        if not isinstance(item, dict):
            raise ValueError(f"query[{i}] must be a mapping")
        queries.append(
            Query(
                id=str(item["id"]),
                nl_query=str(item.get("nl_query", "")),
                expected_sql=str(item.get("expected_sql", "")),
                disabled=bool(item.get("disabled", False)),
                equivalent_attributes=list(item.get("equivalent_attributes") or []),
            )
        )

    return Corpus(context=context, queries=queries, path=p)
