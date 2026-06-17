"""Run loop — invoke agent per case, collect AgentAnswers, build RunRecord."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import AgentAnswer, ParseError, TabularAnswer, parse_answer
from .corpus import Corpus, Query
from .oracle_cli import CliOracleConfig, cli_precheck, execute_golden_cli
from .oracle_pgwire import (
    PgwireConfig,
    ReferenceTable,
    execute_golden,
    pgwire_precheck,
    substitute_placeholders,
)
from .record import ROW_SAMPLE_CAP, CaseRecord, RunRecord, new_record, write_record
from .registry import AgentEntry
from .scoring import score_case


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
        timeout=360,
    )
    return result.stdout


def _fixture_verdict(answer: AgentAnswer) -> str:
    """Derive a raw verdict from the answer type (offline/fixture mode — no oracle)."""
    from .contract import CannotAnswer

    if isinstance(answer, CannotAnswer):
        return "no_bind"
    return "wrong"  # tabular/handle/scalar without oracle = cannot score


@dataclass
class _RepResult:
    """Single-rep scoring result with diagnostic fields."""
    verdict: str
    answer: AgentAnswer | None
    detail: str | None
    row_recall: float | None
    jaccard: float | None
    column_recall: float | None
    column_jaccard: float | None
    reference: ReferenceTable | None  # None for fixture/error/oversize


def _score_one_rep(
    entry: AgentEntry,
    query: Query,
    context: str,
    model: str,
    oracle_mode: str,
    cfg: PgwireConfig | CliOracleConfig | None,
    pass_threshold: float,
) -> _RepResult:
    """Invoke agent once and score, returning diagnostic fields."""

    try:
        raw = _invoke_agent(entry, query, context, model)
        answer: AgentAnswer = parse_answer(raw)
    except ParseError as exc:
        return _RepResult("parse_error", None, str(exc), None, None, None, None, None)
    except subprocess.TimeoutExpired:
        return _RepResult("parse_error", None, "agent timed out", None, None, None, None, None)

    if oracle_mode == "fixture":
        verdict = _fixture_verdict(answer)
        return _RepResult(verdict, answer, None, None, None, None, None, None)

    assert cfg is not None  # precheck already ran; cfg is set

    if oracle_mode == "pgwire":
        assert isinstance(cfg, PgwireConfig)
        substituted_sql = substitute_placeholders(
            query.expected_sql,
            cfg.catalog_name,
            cfg.model_name,
        )
        reference = execute_golden(cfg, query.id, substituted_sql)
    else:
        # oracle == "cli"
        assert isinstance(cfg, CliOracleConfig)
        reference = execute_golden_cli(cfg, query.id, query.expected_sql)

    equiv = query.equivalent_attributes or []
    value_equiv = query.equivalent_values or {}
    result = score_case(
        reference, answer, pass_threshold=pass_threshold,
        equiv=equiv, value_equiv=value_equiv,
    )

    row_recall: float | None = None
    jaccard: float | None = None
    column_recall: float | None = None
    column_jaccard: float | None = None
    if result.metrics is not None:
        row_recall = result.metrics.row_recall
        jaccard = result.metrics.row_jaccard
        column_recall = result.metrics.column_recall
        column_jaccard = result.metrics.column_jaccard

    # Only retain reference table for sampling when it's a real ReferenceTable
    ref_table = reference if isinstance(reference, ReferenceTable) else None

    return _RepResult(
        verdict=result.verdict,
        answer=answer,
        detail=result.detail,
        row_recall=row_recall,
        jaccard=jaccard,
        column_recall=column_recall,
        column_jaccard=column_jaccard,
        reference=ref_table,
    )


def _build_diagnostic_fields(
    answer: AgentAnswer | None,
    reference: ReferenceTable | None,
    cap: int = ROW_SAMPLE_CAP,
) -> dict[str, Any]:
    """Extract bounded diagnostic fields from a scored rep for CaseRecord."""
    fields: dict[str, Any] = {}

    # Reference table diagnostics
    if reference is not None:
        ref_rows = reference.rows
        fields["reference_columns"] = list(reference.columns)
        fields["row_count_reference"] = len(ref_rows)
        if len(ref_rows) > cap:
            fields["reference_rows_sample"] = [list(r) for r in ref_rows[:cap]]
            fields["sample_truncated"] = True
        else:
            fields["reference_rows_sample"] = [list(r) for r in ref_rows]
            # sample_truncated stays None/False unless candidate also truncates

    # Candidate table diagnostics (only TabularAnswer carries column/row data)
    if isinstance(answer, TabularAnswer):
        cand_rows = answer.rows
        fields["candidate_columns"] = list(answer.columns)
        fields["row_count_candidate"] = len(cand_rows)
        if len(cand_rows) > cap:
            fields["candidate_rows_sample"] = [list(r) for r in cand_rows[:cap]]
            fields["sample_truncated"] = True
        else:
            fields["candidate_rows_sample"] = [list(r) for r in cand_rows]

    return fields


def run_corpus(
    corpus: Corpus,
    agent_entry: AgentEntry,
    model: str,
    server: str,
    results_dir: Path,
    config: dict[str, Any],
) -> tuple[RunRecord, Path]:
    """Run all active corpus cases through the agent and write a RunRecord."""
    oracle_mode: str = config.get("oracle", "fixture")
    pass_threshold: float = float(config.get("pass_threshold", 0.95))
    repeat: int = int(config.get("repeat", 1))
    min_pass_reps_cfg = config.get("min_pass_reps")
    min_pass_reps: int = (
        int(min_pass_reps_cfg) if min_pass_reps_cfg is not None else repeat
    )

    corpus_id = corpus.path.stem
    record, _ = new_record(
        agent=agent_entry.name,
        server=server,
        corpus_id=corpus_id,
        config=config,
    )

    # Build oracle config and run precheck once (fast-fail before case loop)
    pgwire_cfg: PgwireConfig | CliOracleConfig | None = None
    if oracle_mode == "pgwire":
        pg_host: str = config.get("pg_host", "localhost")
        pg_pass_env: str = config.get("pg_pass_env", "ATSCALE_PG_PASS")
        catalog_name: str = config.get("catalog_name", "atscale_catalogs")
        model_name: str = config.get("model_name", "tpcds_benchmark_model")
        pg_user: str = config.get("pg_user") or os.environ.get("ATSCALE_PG_USER", "atscale")
        pg_dbname: str = config.get("pg_dbname", "atscale_catalogs")
        pgwire_cfg = PgwireConfig(
            pg_host=pg_host,
            pg_user=pg_user,
            pg_pass_env=pg_pass_env,
            pg_dbname=pg_dbname,
            catalog_name=catalog_name,
            model_name=model_name,
        )
        pgwire_precheck(pgwire_cfg)  # raises RuntimeError on failure → propagates up
    elif oracle_mode == "cli":
        gold_query_cmd: str = config.get("gold_query_cmd", "mqo-pg-query")
        cli_endpoint: str = config.get("cli_endpoint", "")
        cli_catalog: str = config.get("catalog_name", "atscale_catalogs")
        cli_model: str = config.get("model_name", "tpcds_benchmark_model")
        cli_timeout: int = int(config.get("cli_timeout_s", 120))
        cli_cfg = CliOracleConfig(
            gold_query_cmd=gold_query_cmd,
            endpoint=cli_endpoint,
            catalog_name=cli_catalog,
            model_name=cli_model,
            timeout_s=cli_timeout,
        )
        cli_precheck(cli_cfg)  # raises RuntimeError on failure → propagates up
        pgwire_cfg = cli_cfg

    # Skipped cases
    for q in corpus.skipped:
        record.cases.append(CaseRecord(id=q.id, nl_query=q.nl_query, verdict="skipped"))

    # Active cases
    for q in corpus.active:
        t0 = time.monotonic()
        rep_verdicts_list: list[str] | None = None

        if repeat == 1:
            rep = _score_one_rep(
                agent_entry, q, corpus.context, model,
                oracle_mode, pgwire_cfg, pass_threshold,
            )
            verdict = rep.verdict
            answer = rep.answer
            detail = rep.detail
            row_recall = rep.row_recall
            jaccard = rep.jaccard
            column_recall = rep.column_recall
            column_jaccard = rep.column_jaccard
            diag = _build_diagnostic_fields(rep.answer, rep.reference)
            answer_type = answer.answer_type if answer is not None else None
        else:
            # k-of-n: run repeat times, aggregate
            rep_verdicts_list = []
            last_rep: _RepResult | None = None
            last_detail = None
            agg_row_recall: list[float] = []
            agg_jaccard: list[float] = []
            agg_column_recall: list[float] = []
            agg_column_jaccard: list[float] = []

            for _rep in range(repeat):
                rep = _score_one_rep(
                    agent_entry, q, corpus.context, model,
                    oracle_mode, pgwire_cfg, pass_threshold,
                )
                rep_verdicts_list.append(rep.verdict)
                last_rep = rep
                last_detail = rep.detail
                if rep.row_recall is not None:
                    agg_row_recall.append(rep.row_recall)
                if rep.jaccard is not None:
                    agg_jaccard.append(rep.jaccard)
                if rep.column_recall is not None:
                    agg_column_recall.append(rep.column_recall)
                if rep.column_jaccard is not None:
                    agg_column_jaccard.append(rep.column_jaccard)

            correct_reps = rep_verdicts_list.count("correct")
            verdict = "correct" if correct_reps >= min_pass_reps else "wrong"
            last_answer = last_rep.answer if last_rep is not None else None
            answer_type = last_answer.answer_type if last_answer is not None else None
            detail = last_detail
            row_recall = (
                sum(agg_row_recall) / len(agg_row_recall) if agg_row_recall else None
            )
            jaccard = sum(agg_jaccard) / len(agg_jaccard) if agg_jaccard else None
            column_recall = (
                sum(agg_column_recall) / len(agg_column_recall) if agg_column_recall else None
            )
            column_jaccard = (
                sum(agg_column_jaccard) / len(agg_column_jaccard) if agg_column_jaccard else None
            )
            # Use the last rep's data for diagnostic samples
            diag = _build_diagnostic_fields(
                last_rep.answer if last_rep else None,
                last_rep.reference if last_rep else None,
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        record.cases.append(
            CaseRecord(
                id=q.id,
                nl_query=q.nl_query,
                verdict=verdict,
                answer_type=answer_type,
                detail=detail,
                row_recall=row_recall,
                column_recall=column_recall,
                column_jaccard=column_jaccard,
                jaccard=jaccard,
                rep_verdicts=rep_verdicts_list,
                latency_ms=latency_ms,
                candidate_columns=diag.get("candidate_columns"),
                reference_columns=diag.get("reference_columns"),
                candidate_rows_sample=diag.get("candidate_rows_sample"),
                reference_rows_sample=diag.get("reference_rows_sample"),
                row_count_candidate=diag.get("row_count_candidate"),
                row_count_reference=diag.get("row_count_reference"),
                sample_truncated=diag.get("sample_truncated"),
            )
        )

    from .record import _now_iso

    record.finalise(_now_iso())
    dest = write_record(record, results_dir)
    return record, dest
