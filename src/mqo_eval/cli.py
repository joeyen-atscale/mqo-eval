"""mqo-eval CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from .corpus import load_corpus
    from .registry import RegistryError, load_registry, resolve_agent
    from .runner import run_corpus

    try:
        corpus = load_corpus(args.corpus)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    registry_path = Path(args.agents_yaml)
    registry = load_registry(registry_path)
    try:
        agent_entry = resolve_agent(args.agent, registry)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results_dir = Path(args.results_dir)
    config = {
        "agent": args.agent,
        "model": args.model,
        "server": args.server,
        "corpus": str(args.corpus),
        "oracle": args.oracle,
        "pg_host": args.pg_host,
        "pg_sslmode": args.pg_sslmode,
        "pg_pass_env": args.pg_pass_env,
        "pg_user": args.pg_user,
        "pg_dbname": args.pg_dbname,
        "catalog_name": args.catalog_name,
        "model_name": args.model_name,
        "gold_query_cmd": args.gold_query_cmd,
        "cli_endpoint": args.cli_endpoint,
        "gold_file": args.gold_file or "",
        "pass_threshold": args.pass_threshold,
        "repeat": args.repeat,
        "min_pass_reps": (
            args.min_pass_reps if args.min_pass_reps is not None else args.repeat
        ),
        # Selective-retest policy (PRD-mqoeval-selective-retest)
        "skip_stable": args.skip_stable,  # None = off (default)
        "full_every": args.full_every,
    }

    if corpus.active:
        print(
            f"running {len(corpus.active)} active cases "
            f"({len(corpus.skipped)} skipped) via agent '{agent_entry.name}'"
        )
    else:
        print("no active cases (all disabled)")

    if args.skip_stable is not None:
        print(
            f"selective-retest: --skip-stable {args.skip_stable} "
            f"--full-every {args.full_every}"
        )

    record, dest = run_corpus(
        corpus=corpus,
        agent_entry=agent_entry,
        model=args.model,
        server=args.server,
        results_dir=results_dir,
        config=config,
    )

    s = record.summary
    if s.active == 0:
        print("summary: no active cases")
    else:
        # R6: label pass rate with tested/carried split when selective-retest is active
        if s.carried > 0:
            pass_rate_label = (
                f"pass rate: {s.correct}/{s.active} ({s.accuracy:.0%})"
                f" — {s.tested} tested, {s.carried} carried"
            )
        else:
            pass_rate_label = f"pass rate: {s.correct}/{s.active} ({s.accuracy:.0%})"
        print(
            f"summary: {pass_rate_label} | wrong={s.wrong} no_bind={s.no_bind} "
            f"parse_errors={s.parse_errors} skipped={s.skipped}"
        )
    print(f"record: {dest}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    path = Path(args.results)
    if not path.exists():
        print(f"error: results file not found: {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text())
    cfg = data.get("config", {})
    repeat = int(cfg.get("repeat", 1))
    min_pass_reps = int(cfg.get("min_pass_reps", repeat))
    summary = data.get("summary", {})
    active = summary.get("active", 0)
    if active == 0:
        print("no active cases")
        return 0
    pct = f"{summary.get('accuracy', 0):.0%}"
    correct = summary.get("correct", 0)
    tested = summary.get("tested", 0)
    carried = summary.get("carried", 0)
    # Label as "capability pass rate" when k>1 to distinguish from single-shot
    rate_label = "capability pass rate" if repeat > 1 else "pass rate"
    if carried > 0:
        pass_rate_line = (
            f"{rate_label}: {correct}/{active} ({pct})"
            f" — {tested} tested, {carried} carried"
        )
    else:
        pass_rate_line = f"{rate_label}: {correct}/{active} ({pct})"
    print(pass_rate_line)
    if repeat > 1:
        print(f"gate:        k={repeat}, min_pass_reps={min_pass_reps}")
    print(f"wrong:       {summary.get('wrong', 0)}")
    print(f"no_bind:     {summary.get('no_bind', 0)}")
    print(f"parse_error: {summary.get('parse_errors', 0)}")
    print(f"skipped:     {summary.get('skipped', 0)}")
    if carried > 0:
        print(f"carried:     {carried}")
    recall = summary.get("mean_row_recall")
    if recall is not None:
        print(f"mean_recall: {recall:.3f}")
    jaccard = summary.get("mean_row_jaccard")
    if jaccard is not None:
        print(f"mean_jaccard: {jaccard:.3f}")
    # When k>1: list unstable cases (non-unanimous rep_verdicts) as next-build targets
    if repeat > 1:
        cases = data.get("cases", [])
        unstable = []
        for c in cases:
            rv = c.get("rep_verdicts")
            if rv and len(rv) > 1 and len(set(rv)) > 1:
                verdict_str = "".join("+" if v == "correct" else "-" for v in rv)
                correct_cnt = rv.count("correct")
                gate = "PASS" if correct_cnt >= min_pass_reps else "FAIL"
                unstable.append(f"  {c['id']} [{verdict_str}] {gate}")
        if unstable:
            print(f"unstable:    {len(unstable)} case(s) with mixed reps (next-build targets):")
            for line in unstable:
                print(line)
        else:
            print("unstable:    none (all reps unanimous)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mqo-eval", description="MQO eval harness")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run a corpus through an agent")
    run_p.add_argument("--corpus", required=True, help="path to corpus YAML")
    run_p.add_argument("--agent", default="stub", help="agent name or path")
    run_p.add_argument(
        "--model", default="tpcds_benchmark_model", help="model coordinate"
    )
    run_p.add_argument("--server", default="fixture", help="server label")
    run_p.add_argument("--results-dir", default="results", help="archive root")
    run_p.add_argument("--agents-yaml", default="agents.yaml", help="registry file")
    run_p.add_argument(
        "--oracle",
        choices=["fixture", "pgwire", "cli", "precomputed"],
        default="fixture",
        help=(
            "oracle backend: fixture (offline/no scoring), pgwire (live PGWire/psycopg2), "
            "cli (shell out to mqo-pg-query), or precomputed (pre-minted gold cache JSON)"
        ),
    )
    run_p.add_argument(
        "--gold-file",
        default=None,
        help="path to pre-minted gold cache JSON (required with --oracle precomputed)",
    )
    run_p.add_argument(
        "--pg-host",
        default="localhost",
        help="PGWire host (only used with --oracle pgwire)",
    )
    run_p.add_argument("--pg-user", default=None, help="PGWire user (default: $ATSCALE_PG_USER or 'atscale')")
    run_p.add_argument("--pg-dbname", default="atscale_catalogs", help="PGWire dbname (AtScale catalog)")
    run_p.add_argument(
        "--pg-sslmode",
        default="require",
        choices=["require", "disable", "allow", "prefer"],
        help="PGWire sslmode (default: require; use disable for local Docker Community Edition)",
    )
    run_p.add_argument(
        "--pg-pass-env",
        default="ATSCALE_PG_PASS",
        help="env var name holding PGWire password",
    )
    run_p.add_argument(
        "--catalog-name",
        default="atscale_catalogs",
        help="AtScale catalog name for SQL placeholder substitution",
    )
    run_p.add_argument(
        "--model-name",
        default="tpcds_benchmark_model",
        help="AtScale model name for SQL placeholder substitution",
    )
    run_p.add_argument(
        "--pass-threshold",
        type=float,
        default=0.95,
        help="row-recall threshold for a correct verdict (default: 0.95)",
    )
    run_p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run each case this many times (k-of-n)",
    )
    run_p.add_argument(
        "--min-pass-reps",
        type=int,
        default=None,
        help="minimum correct reps required for overall correct (default: --repeat)",
    )
    run_p.add_argument(
        "--gold-query-cmd",
        default="mqo-pg-query",
        help=(
            "path or name of the mqo-pg-query binary (only used with --oracle cli; "
            "default: 'mqo-pg-query' on PATH)"
        ),
    )
    run_p.add_argument(
        "--cli-endpoint",
        default="",
        help=(
            "AtScale PGWire endpoint (host:port) passed to mqo-pg-query "
            "(only used with --oracle cli)"
        ),
    )
    run_p.add_argument(
        "--skip-stable",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Skip cases with N consecutive correct verdicts in the archive "
            "(carry forward their last result). Off by default. "
            "Use with --full-every to set recalibration cadence."
        ),
    )
    run_p.add_argument(
        "--full-every",
        type=int,
        default=5,
        metavar="M",
        help=(
            "Force a full run (no skips) every M runs, for recalibration. "
            "Default: 5. Set to 0 to disable cadence (skip whenever streak qualifies). "
            "Only used when --skip-stable is set."
        ),
    )

    sum_p = sub.add_parser("summary", help="summarise a run record")
    sum_p.add_argument("--results", required=True, help="path to RunRecord JSON")

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        sys.exit(_cmd_run(args))
    elif args.command == "summary":
        sys.exit(_cmd_summary(args))
