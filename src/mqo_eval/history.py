"""Pass-history reader for selective-retest (PRD-mqoeval-selective-retest).

Reads archived run-record JSON files to determine per-case consecutive-correct
streak lengths.  Used by runner.py to decide which cases to skip.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def load_pass_history(
    results_dir: Path,
    corpus_id: str,
    agent: str,
    server: str,
    n: int,
) -> dict[str, int]:
    """Return {case_id: consecutive_correct_streak} over the newest *n* runs.

    Scans results_dir/<agent>/<server>/<corpus_id>/*.json, sorts by started_at
    desc (newest first), reads up to *n* files, and for each case_id counts how
    many consecutive newest-first verdicts are "correct".

    Tolerates missing/corrupt JSON (R7): bad files are skipped with a warning.
    Returns an empty dict when no archive is found.
    """
    archive_dir = results_dir / agent / server / corpus_id
    if not archive_dir.is_dir():
        return {}

    # Collect candidate files sorted by started_at desc (newest first)
    candidates: list[tuple[str, Path]] = []
    for path in archive_dir.glob("*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            started_at = obj.get("started_at", "")
            candidates.append((started_at, path))
        except Exception as exc:
            log.warning("history: skipping corrupt archive %s: %s", path, exc)

    candidates.sort(key=lambda t: t[0], reverse=True)
    newest_n = candidates[:n]

    # Per-case verdict lists (newest first)
    case_verdicts: dict[str, list[str]] = {}
    for _ts, path in newest_n:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            for case in obj.get("cases", []):
                cid = case.get("id")
                verdict = case.get("verdict")
                if cid and verdict:
                    case_verdicts.setdefault(cid, []).append(verdict)
        except Exception as exc:
            log.warning("history: skipping corrupt archive %s: %s", path, exc)

    # Count consecutive correct from the front (newest first)
    streaks: dict[str, int] = {}
    for cid, verdicts in case_verdicts.items():
        streak = 0
        for v in verdicts:
            if v == "correct":
                streak += 1
            else:
                break
        streaks[cid] = streak

    return streaks


def prior_run_count(
    results_dir: Path,
    corpus_id: str,
    agent: str,
    server: str,
) -> int:
    """Return the number of archived runs for this (corpus_id, agent, server)."""
    archive_dir = results_dir / agent / server / corpus_id
    if not archive_dir.is_dir():
        return 0
    return sum(1 for _ in archive_dir.glob("*.json"))
