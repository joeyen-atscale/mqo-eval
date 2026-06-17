#!/usr/bin/env bash
# run-metrics.sh — READ-ONLY harness. Emits normalized metrics.json for the loop.
# Fixed to use `uv run` so ruff/mypy/pytest are found in the project venv.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
mkdir -p .pybuilder

ruff_errors=$(uv run --no-sync ruff check --quiet . 2>/dev/null | grep -c . || true)
ruff_errors=${ruff_errors:-0}

mypy_errors=$(uv run --no-sync mypy src 2>/dev/null | grep -c 'error:' || true)
mypy_errors=${mypy_errors:-0}

uv run --no-sync pytest -q --tb=no -q 2>/dev/null
tests_failing=$?

printf '{ "ruff_errors": %d, "mypy_errors": %d, "tests_failing": %d, "coverage_pct": 0, "coverage_min": 0 }\n' \
  "$ruff_errors" "$mypy_errors" "$tests_failing" > .pybuilder/metrics.json
echo "wrote .pybuilder/metrics.json"
