"""CI floor gate for rule-violation rates (FR4).

Reads corpus/rule_violations/cases.yaml, runs each case through
RuleViolationHarness, computes per-rule violation rate, and exits:
  - 0 if ALL migrated rules satisfy their configured floor (default ≤ 1 %)
  - 1 with a report if any rule exceeds its floor

Usage (from the mqo-eval root)::

    uv run python -m mqo_eval.rule_ci_floor
    uv run python -m mqo_eval.rule_ci_floor --report-only   # never exit 1
    uv run python -m mqo_eval.rule_ci_floor --rule R-MS     # single rule

Violation rate definition (FR2)::

    violation_rate = (violating cases where verdict == "fail") / (total violating cases)

i.e. the fraction of known-bad queries that the harness CORRECTLY catches.
A rate of 0 % means nothing is being caught; 100 % means all violations caught.

The CI floor fails when violation_rate < floor_pct for a MIGRATED rule
(meaning the enforcement has regressed and violating queries are slipping through).

Wait — re-reading the PRD: the floor is meant to ensure the violation rate stays
*near 0 %* AFTER migration. In other words, the migration moves enforcement from
LLM prose to the server, so after migration, the server catches violations before
the LLM ever sees them. The measurable outcome is: "violating test cases are
BLOCKED (verdict == fail) at ~100 %, and conforming cases are NOT blocked."

The CI gate (FR4) thus fires when:
    (violating cases admitted / total violating cases) > floor_pct
i.e. when too many bad queries are PASSING THROUGH (not being caught).

We call this the *admission rate*: fraction of violating cases that the checker
lets through (verdict == "pass"). The floor is the maximum tolerated admission rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .rule_harness import RuleViolationHarness, is_live_eval_running

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Per-rule admission-rate floor (fraction of violating cases that may slip through).
# 0.01 = 1 % threshold.  Override per-rule here as rules are migrated.
RULE_FLOORS: dict[str, float] = {
    "R-MS": 0.01,   # migrated: server-validator, hard floor
    "R-FQ": 0.01,   # migrated: server-validator (PR #79 reference check)
    "R-CM": 0.05,   # not yet migrated: softer floor
    "R-AG": 0.05,
    "R-SD": 0.10,
    "R-DR": 0.10,
    "R-OB": 0.10,
    "R-NL": 1.00,   # intent-only: no hard floor
    "R-SJ": 0.10,
    "R-PH": 1.00,   # intent-only: no hard floor
}

DEFAULT_FLOOR = 0.10  # for any rule not in the table above

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    rule_ids: list[str]
    expected_verdict: str  # "violating" | "conforming" | "edge"
    sql: str
    lm_driven: bool
    per_rule: dict[str, str] = field(default_factory=dict)  # rule_id -> verdict


@dataclass
class RuleStats:
    rule_id: str
    total_violating: int = 0
    admitted: int = 0        # violating cases that PASSED (slipped through)
    total_conforming: int = 0
    false_rejected: int = 0  # conforming cases that FAILED (false positive)
    skipped_lm: int = 0

    @property
    def admission_rate(self) -> float | None:
        if self.total_violating == 0:
            return None
        return self.admitted / self.total_violating

    @property
    def false_positive_rate(self) -> float | None:
        if self.total_conforming == 0:
            return None
        return self.false_rejected / self.total_conforming


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------

_CORPUS_PATH = (
    Path(__file__).parent.parent.parent
    / "corpus"
    / "rule_violations"
    / "cases.yaml"
)


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or _CORPUS_PATH
    with open(p) as fh:
        data = yaml.safe_load(fh)
    return data.get("cases", [])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_floor_check(
    rule_filter: str | None = None,
    report_only: bool = False,
    corpus_path: Path | None = None,
) -> tuple[bool, list[RuleStats]]:
    """Run the CI floor check.

    Returns (passed: bool, stats: list[RuleStats]).
    ``passed`` is True when all rules are within their floors.
    """
    if is_live_eval_running():
        print(
            "ERROR: A live eval run is in progress (tick.lock held). "
            "Refusing to run concurrently per FR7.",
            file=sys.stderr,
        )
        sys.exit(2)

    cases = load_cases(corpus_path)
    harness = RuleViolationHarness()

    # Collect per-rule stats.
    stats: dict[str, RuleStats] = {}

    for raw in cases:
        case_id: str = raw["id"]
        rule_ids: list[str] = raw.get("rule_ids", [])
        expected: str = raw.get("verdict", "conforming")
        sql: str = raw.get("sql", "")
        lm_driven: bool = raw.get("lm_driven", False)

        # Filter by rule if requested.
        if rule_filter and rule_filter not in rule_ids:
            continue

        for rid in rule_ids:
            if rid not in stats:
                stats[rid] = RuleStats(rule_id=rid)
            st = stats[rid]

            verdict_obj = harness.check_rule_violations(sql, rid)

            if verdict_obj.lm_driven or lm_driven:
                st.skipped_lm += 1
                continue

            v = verdict_obj.verdict

            if expected == "violating":
                st.total_violating += 1
                if v == "pass":
                    st.admitted += 1  # violation slipped through
            elif expected == "conforming":
                st.total_conforming += 1
                if v == "fail":
                    st.false_rejected += 1
            # edge cases: counted in skipped for now
            else:
                st.skipped_lm += 1

    if not stats:
        print("No cases matched. Corpus may be empty or filter too narrow.")
        print("Exiting cleanly per AC7 (no cases → no floor violation).")
        return True, []

    # Evaluate floors and build report.
    all_pass = True
    stats_list = sorted(stats.values(), key=lambda s: s.rule_id)

    print()
    print(f"{'Rule':<10}  {'Admiss.Rate':>12}  {'FP.Rate':>9}  {'Floor':>7}  {'Status'}")
    print("-" * 60)

    failures: list[str] = []
    for st in stats_list:
        floor = RULE_FLOORS.get(st.rule_id, DEFAULT_FLOOR)
        ar = st.admission_rate
        fp = st.false_positive_rate

        ar_str = f"{ar:.1%}" if ar is not None else "N/A"
        fp_str = f"{fp:.1%}" if fp is not None else "N/A"

        if ar is None:
            status = "N/A (no violating cases)"
        elif ar <= floor:
            status = "PASS"
        else:
            status = f"FAIL (>{floor:.1%})"
            all_pass = False
            failures.append(
                f"  {st.rule_id}: admission_rate={ar:.1%} exceeds floor {floor:.1%} "
                f"({st.admitted}/{st.total_violating} violating cases slipped through)"
            )

        print(f"{st.rule_id:<10}  {ar_str:>12}  {fp_str:>9}  {floor:>7.1%}  {status}")

    print()

    if failures:
        print("FLOOR VIOLATIONS:")
        for f in failures:
            print(f)
        print()

    if report_only and not all_pass:
        print("Running in --report-only mode; suppressing exit 1.")
        all_pass = True

    return all_pass, stats_list


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CI floor gate: checks per-rule violation admission rates against configured floors."
    )
    parser.add_argument(
        "--rule", metavar="RULE_ID", help="Only check this rule (e.g. R-MS)"
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print the report but never exit 1 (introduce the gate in report mode first).",
    )
    parser.add_argument(
        "--corpus",
        metavar="PATH",
        help="Path to cases.yaml (defaults to corpus/rule_violations/cases.yaml).",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        metavar="PATH",
        help="Write structured JSON report to PATH.",
    )
    args = parser.parse_args()

    corpus_path = Path(args.corpus) if args.corpus else None

    passed, stats_list = run_floor_check(
        rule_filter=args.rule,
        report_only=args.report_only,
        corpus_path=corpus_path,
    )

    if args.json_out:
        report = {
            "passed": passed,
            "rules": [
                {
                    "rule_id": s.rule_id,
                    "total_violating": s.total_violating,
                    "admitted": s.admitted,
                    "admission_rate": s.admission_rate,
                    "total_conforming": s.total_conforming,
                    "false_rejected": s.false_rejected,
                    "false_positive_rate": s.false_positive_rate,
                    "skipped_lm": s.skipped_lm,
                    "floor": RULE_FLOORS.get(s.rule_id, DEFAULT_FLOOR),
                }
                for s in stats_list
            ],
        }
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"JSON report written to {args.json_out}")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
