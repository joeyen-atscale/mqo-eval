"""RunRecord — per-case results + archive writer."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _make_run_id(corpus_id: str) -> str:
    ts = _now_iso()
    return f"{ts}-{corpus_id[:8]}"


@dataclass
class CaseRecord:
    id: str
    nl_query: str
    verdict: str  # "correct"|"wrong"|"no_bind"|"parse_error"|"skipped"|"oversize"
    answer_type: str | None = None
    detail: str | None = None
    row_recall: float | None = None
    column_recall: float | None = None
    jaccard: float | None = None
    rep_verdicts: list[str] | None = None
    latency_ms: int = 0


@dataclass
class SummaryStats:
    total: int
    active: int
    skipped: int
    correct: int
    wrong: int
    no_bind: int
    parse_errors: int
    mean_row_recall: float | None = None
    mean_row_jaccard: float | None = None

    @property
    def accuracy(self) -> float:
        return self.correct / self.active if self.active else 0.0


@dataclass
class RunRecord:
    run_id: str
    started_at: str
    finished_at: str
    agent: str
    server: str
    corpus_id: str
    config: dict[str, Any]
    cases: list[CaseRecord] = field(default_factory=list)
    summary: SummaryStats = field(
        default_factory=lambda: SummaryStats(0, 0, 0, 0, 0, 0, 0)
    )

    def finalise(self, finished_at: str) -> None:
        self.finished_at = finished_at
        active = [c for c in self.cases if c.verdict != "skipped"]
        skipped = [c for c in self.cases if c.verdict == "skipped"]
        correct = [c for c in active if c.verdict == "correct"]
        wrong = [c for c in active if c.verdict == "wrong"]
        no_bind = [c for c in active if c.verdict == "no_bind"]
        parse_err = [c for c in active if c.verdict == "parse_error"]
        recalls = [c.row_recall for c in active if c.row_recall is not None]
        jaccards = [c.jaccard for c in active if c.jaccard is not None]
        self.summary = SummaryStats(
            total=len(self.cases),
            active=len(active),
            skipped=len(skipped),
            correct=len(correct),
            wrong=len(wrong),
            no_bind=len(no_bind),
            parse_errors=len(parse_err),
            mean_row_recall=sum(recalls) / len(recalls) if recalls else None,
            mean_row_jaccard=sum(jaccards) / len(jaccards) if jaccards else None,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # flatten summary
        d["summary"] = asdict(self.summary)
        d["summary"]["accuracy"] = self.summary.accuracy
        return d


def write_record(record: RunRecord, results_dir: Path) -> Path:
    """Write record atomically to results/<agent>/<server>/<corpus>/<run_id>.json."""
    dest_dir = results_dir / record.agent / record.server / record.corpus_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{record.run_id}.json"
    if dest.exists():
        raise FileExistsError(
            f"run record already exists (refusing to overwrite): {dest}"
        )
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2))
    os.rename(tmp, dest)
    return dest


def new_record(
    agent: str,
    server: str,
    corpus_id: str,
    config: dict[str, Any],
) -> tuple[RunRecord, str]:
    started_at = _now_iso()
    run_id = _make_run_id(corpus_id)
    record = RunRecord(
        run_id=run_id,
        started_at=started_at,
        finished_at=started_at,
        agent=agent,
        server=server,
        corpus_id=corpus_id,
        config=config,
    )
    return record, started_at
