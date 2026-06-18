#!/usr/bin/env bash
# Runs one mqo-eval cycle and appends the result to ledger.md.
# Launch detached: setsid bash run-eval-and-digest.sh >> eval.log 2>&1 < /dev/null &
# flock-guarded: a second invocation while one is running is a no-op.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK="$SCRIPT_DIR/.eval.lock"
LEDGER="$SCRIPT_DIR/ledger.md"
RESULTS_DIR="$SCRIPT_DIR/results"
STOP="$SCRIPT_DIR/STOP"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -u +%Y%m%dT%H%M%SZ) eval already running, exiting"
  exit 0
fi

if [[ -e "$STOP" ]]; then
  echo "$(date -u +%Y%m%dT%H%M%SZ) STOP sentinel present, skipping"
  exit 0
fi

# Load credentials
if [[ -f "$HOME/projects/mqo-demo/.env" ]]; then
  set -a; source "$HOME/projects/mqo-demo/.env"; set +a
fi
if [[ -f "$HOME/.config/mcp-watch/secrets.env" ]]; then
  set -a; source "$HOME/.config/mcp-watch/secrets.env"; set +a
fi

# Tell mqo-pg-query to use direct PGWire password (bypasses OIDC service token)
export ATSCALE_PG_PASS_ENV=ATSCALE_PG_PASS

# Tell the claude-oauth agent where the catalog snapshot lives
export MQO_CATALOG_PATH="$HOME/projects/mqo-mcp/mqo-mcp-server/fixtures/tpcds_catalog.json"

# Live AtScale cluster — required so mqo-mcp-server connects to mcp-aws (not fixture engine)
export MQO_ENDPOINT="mcp-aws.atscaleinternal.com:15432"
export MQO_XMLA_URL="https://mcp-aws.atscaleinternal.com/v1/xmla"
export MQO_OIDC_TOKEN_URL="https://mcp-aws.atscaleinternal.com/auth/realms/atscale/protocol/openid-connect/token"
export MQO_OIDC_CLIENT_ID="atscale-mcp"
export MQO_OIDC_REALM="atscale"
# ATSCALE_OIDC_SECRET is sourced above from secrets.env / mqo-demo/.env

START_TS="$(date -u +%Y%m%dT%H%M%SZ)"
echo "$START_TS eval start"

cd "$EVAL_DIR"

TMPOUT="$(mktemp)"
EVAL_EXIT=0
# k=3 capability gate: each case runs 3 times; correct iff ≥2/3 reps pass (majority).
# --skip-stable 3: cases with 3 consecutive prior correct runs are carried forward
# (selective-retest, shipped), bounding total agent calls to ~30 instead of 60.
uv run mqo-eval run \
  --corpus corpus/tpcds_sql_derived_limited.yaml \
  --agent claude-oauth \
  --server mcp-aws-live \
  --oracle cli \
  --pg-pass-env ATSCALE_PG_PASS \
  --catalog-name '"atscale_catalogs"."tpcds_Snowflake"' \
  --model-name '"tpcds_benchmark_model"' \
  --results-dir "$RESULTS_DIR" \
  --repeat 3 \
  --min-pass-reps 2 \
  --skip-stable 3 2>&1 | tee "$TMPOUT" || EVAL_EXIT=$?

END_TS="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ $EVAL_EXIT -ne 0 ]]; then
  echo "$END_TS eval FAILED (exit $EVAL_EXIT)"
  rm -f "$TMPOUT"
  exit 1
fi

RECORD_PATH="$(grep '^record: ' "$TMPOUT" | tail -1 | sed 's/^record: //')"
rm -f "$TMPOUT"

echo "$END_TS eval done, record: $RECORD_PATH"

if [[ -z "$RECORD_PATH" || ! -f "$RECORD_PATH" ]]; then
  echo "ERROR: could not find run record, skipping ledger write"
  exit 1
fi

# Get summary (standard pass-rate + counts)
SUMMARY="$(uv run mqo-eval summary --results "$RECORD_PATH" 2>/dev/null || echo "(summary failed)")"

# Compute unstable cases (non-unanimous rep_verdicts) from the record JSON.
# A case is "unstable" when it has rep_verdicts and the verdicts are not all the same.
UNSTABLE_CASES="$(python3 - "$RECORD_PATH" <<'PYEOF'
import json, sys
data = json.loads(open(sys.argv[1]).read())
cfg = data.get("config", {})
repeat = cfg.get("repeat", 1)
min_pass = cfg.get("min_pass_reps", repeat)
cases = data.get("cases", [])
unstable = []
for c in cases:
    rv = c.get("rep_verdicts")
    if rv and len(rv) > 1 and len(set(rv)) > 1:
        verdict_str = "".join("+" if v == "correct" else "-" for v in rv)
        correct_cnt = rv.count("correct")
        gate = "PASS" if correct_cnt >= min_pass else "FAIL"
        unstable.append(f"  {c['id']} [{verdict_str}] {gate}")
if unstable:
    print(f"unstable cases ({len(unstable)}/{len([c for c in cases if c.get('verdict') != 'skipped'])}):")
    for line in unstable:
        print(line)
else:
    print("unstable cases: none (all unanimous)")
PYEOF
)"

# Append ledger entry
{
  echo ""
  echo "## Run — $START_TS → $END_TS"
  echo "- Record: $RECORD_PATH"
  echo "- Gate: k=3, min_pass_reps=2 (majority), skip_stable=3"
  echo ""
  echo '```'
  echo "$SUMMARY"
  echo '```'
  echo ""
  echo "### Unstable cases (next-DREAM targets)"
  echo '```'
  echo "$UNSTABLE_CASES"
  echo '```'
  echo ""
  echo "- Hypotheses: (pending — Haiku digest)"
} >> "$LEDGER"

echo "LEDGER_WRITTEN"
echo "$END_TS ledger appended"
