"""RunRecord — per-case results + archive writer."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _make_run_id(corpus_id: str) -> str:
    ts = _now_iso()
    return f"{ts}-{corpus_id[:8]}"


ROW_SAMPLE_CAP = 10  # max rows stored in candidate/reference samples


@dataclass
class CaseRecord:
    id: str
    nl_query: str
    verdict: str  # "correct"|"wrong"|"no_bind"|"parse_error"|"skipped"|"skipped-stable"|"oversize"
    answer_type: str | None = None
    detail: str | None = None
    row_recall: float | None = None
    column_recall: Optional[float] = None
    column_jaccard: Optional[float] = None
    jaccard: float | None = None
    rep_verdicts: list[str] | None = None
    latency_ms: int = 0
    # Diagnostic fields (G2/FR2): bounded table snapshots for explainability
    candidate_columns: Optional[list[str]] = None
    reference_columns: Optional[list[str]] = None
    candidate_rows_sample: Optional[list[list]] = None
    reference_rows_sample: Optional[list[list]] = None
    row_count_candidate: Optional[int] = None
    row_count_reference: Optional[int] = None
    sample_truncated: Optional[bool] = None
    # Selective-retest (PRD-mqoeval-selective-retest): set when a case is carried
    # forward from a prior run instead of being tested this cycle.
    carried_from_run_id: Optional[str] = None
    # Measureless-gold dedup fields (PRD-mqoeval-dimension-distinct-gold-dedup)
    # gold_deduped: True when the reference was deduplicated before scoring.
    # gold_rows_pre/post: row counts before and after dedup (for auditability).
    gold_deduped: Optional[bool] = None
    gold_rows_pre: Optional[int] = None
    gold_rows_post: Optional[int] = None


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
    # Selective-retest counts (PRD-mqoeval-selective-retest)
    # tested = cases actually run through the agent this cycle
    # carried = cases whose prior correct verdict was carried forward (skipped-stable)
    tested: int = 0
    carried: int = 0

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
        # "skipped" = disabled cases; "skipped-stable" = carried-forward cases (active)
        skipped_disabled = [c for c in self.cases if c.verdict == "skipped"]
        carried = [c for c in self.cases if c.verdict == "skipped-stable"]
        # active = all cases that are not disabled-skipped
        active = [c for c in self.cases if c.verdict != "skipped"]
        # correct includes carried-forward correct cases (honest accounting; R6)
        correct = [c for c in active if c.verdict in ("correct", "skipped-stable")]
        wrong = [c for c in active if c.verdict == "wrong"]
        no_bind = [c for c in active if c.verdict == "no_bind"]
        parse_err = [c for c in active if c.verdict == "parse_error"]
        # Only compute recall/jaccard from actually-scored cases (not carried)
        scored = [c for c in active if c.verdict not in ("skipped-stable",)]
        recalls = [c.row_recall for c in scored if c.row_recall is not None]
        jaccards = [c.jaccard for c in scored if c.jaccard is not None]
        tested_count = len(active) - len(carried)
        self.summary = SummaryStats(
            total=len(self.cases),
            active=len(active),
            skipped=len(skipped_disabled),
            correct=len(correct),
            wrong=len(wrong),
            no_bind=len(no_bind),
            parse_errors=len(parse_err),
            mean_row_recall=sum(recalls) / len(recalls) if recalls else None,
            mean_row_jaccard=sum(jaccards) / len(jaccards) if jaccards else None,
            tested=tested_count,
            carried=len(carried),
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
