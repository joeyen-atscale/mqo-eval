"""Run loop — invoke agent per case, collect AgentAnswers, build RunRecord."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .contract import AgentAnswer, ParseError, parse_answer
from .corpus import Corpus, Query
from .record import CaseRecord, RunRecord, new_record, write_record
from .registry import AgentEntry


def _invoke_agent(entry: AgentEntry, query: Query, context: str, model: str) -> str:
    """Invoke the agent subprocess and return its stdout."""
    env_input = {
        "question": query.nl_query,
        "expected_sql": query.expected_sql,
        "model": model,
        "context": context,
        "case_id": query.id,
    }
    import json
    import os
    env = os.environ.copy()
    env["MQO_EVAL_CASE"] = json.dumps(env_input)

    result = subprocess.run(
        entry.command,
        shell=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.stdout


def _verdict_from_answer(answer: AgentAnswer) -> str:
    from .contract import CannotAnswer, HandleAnswer, ScalarAnswer, TabularAnswer
    if isinstance(answer, CannotAnswer):
        return "no_bind"
    if isinstance(answer, (TabularAnswer, HandleAnswer, ScalarAnswer)):
        return "wrong"  # default until scoring is wired
    return "wrong"


def run_corpus(
    corpus: Corpus,
    agent_entry: AgentEntry,
    model: str,
    server: str,
    results_dir: Path,
    config: dict[str, Any],
) -> tuple[RunRecord, Path]:
    """Run all active corpus cases through the agent and write a RunRecord."""
    corpus_id = corpus.path.stem
    record, _ = new_record(
        agent=agent_entry.name,
        server=server,
        corpus_id=corpus_id,
        config=config,
    )

    # Skipped cases
    for q in corpus.skipped:
        record.cases.append(CaseRecord(id=q.id, nl_query=q.nl_query, verdict="skipped"))

    # Active cases
    for q in corpus.active:
        t0 = time.monotonic()
        raw = ""
        try:
            raw = _invoke_agent(agent_entry, q, corpus.context, model)
            answer: AgentAnswer = parse_answer(raw)
            verdict = _verdict_from_answer(answer)
            detail = None
            answer_type = answer.answer_type
        except ParseError as exc:
            verdict = "parse_error"
            detail = str(exc)
            answer_type = None
        except subprocess.TimeoutExpired:
            verdict = "parse_error"
            detail = "agent timed out"
            answer_type = None

        latency_ms = int((time.monotonic() - t0) * 1000)
        record.cases.append(
            CaseRecord(
                id=q.id,
                nl_query=q.nl_query,
                verdict=verdict,
                answer_type=answer_type,
                detail=detail,
                latency_ms=latency_ms,
            )
        )

    from .record import _now_iso
    record.finalise(_now_iso())
    dest = write_record(record, results_dir)
    return record, dest
