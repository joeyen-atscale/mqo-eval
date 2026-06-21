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

# Operational layer: phase state stamping + quota gate threshold.
source "$SCRIPT_DIR/bl-lib.sh"
NO_IMPROVE_LIMIT="${BL_NO_IMPROVE_LIMIT:-3}"  # pause chain after N evals with no new best

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -u +%Y%m%dT%H%M%SZ) eval already running, exiting"
  exit 0
fi

if [[ -e "$STOP" ]]; then
  echo "$(date -u +%Y%m%dT%H%M%SZ) STOP sentinel present, skipping"
  exit 0
fi

# Load credentials — base layer (may be overridden by docker-local.env below)
if [[ -f "$HOME/projects/mqo-demo/.env" ]]; then
  set -a; source "$HOME/projects/mqo-demo/.env"; set +a
fi
if [[ -f "$HOME/.config/mcp-watch/secrets.env" ]]; then
  set -a; source "$HOME/.config/mcp-watch/secrets.env"; set +a
fi

# Docker / Community Edition overrides — takes precedence over mqo-demo/.env.
# Edit buildloop/docker-local.env to set your Community Edition credentials and
# catalog/model names before running against the local Docker stack.
if [[ -f "$SCRIPT_DIR/docker-local.env" ]]; then
  set -a; source "$SCRIPT_DIR/docker-local.env"; set +a
fi

# Tell the claude-oauth agent where the catalog snapshot lives.
# docker-local.env may override MQO_CATALOG_PATH to a CE-specific fixture;
# fall back to the Snowflake fixture only when not already set.
export MQO_CATALOG_PATH="${MQO_CATALOG_PATH:-$HOME/projects/mqo-mcp/mqo-mcp-server/fixtures/tpcds_catalog.json}"

# Catalog / model name for oracle SQL template substitution.
# Sourced from docker-local.env (DOCKER_CATALOG_NAME / DOCKER_MODEL_NAME).
# Fall back to the live-cluster defaults if not set.
CATALOG_NAME="${DOCKER_CATALOG_NAME:-'"atscale_catalogs"."tpcds_Snowflake"'}"
MODEL_NAME="${DOCKER_MODEL_NAME:-'"tpcds_benchmark_model"'}"
PG_DBNAME="${DOCKER_PG_DBNAME:-atscale_catalogs}"
PG_USER="${ATSCALE_PG_USER:-atscale}"

# Sonnet for eval cases — Haiku cannot reliably invoke the MQO MCP tools (it
# confabulates "TLS handshake" declines), capping the score at ~10%. Sonnet drives
# the tools correctly (10% → 55% headline, ~80% with column-label normalization).
# Override MQO_CLAUDE_MODEL to test a different agent model. (Digest stays Haiku.)
export MQO_CLAUDE_MODEL="${MQO_CLAUDE_MODEL:-claude-sonnet-4-6}"

# ── PREFLIGHT GATE ───────────────────────────────────────────────────────────
# Verify CE + PGWire + the installed binary are healthy BEFORE burning OAuth quota
# on a 20-question run. A broken-infra eval scores ~10% and teaches nothing.
bl_phase "PREFLIGHT" "health gate"
if ! bash "$SCRIPT_DIR/preflight.sh"; then
  PF_REASON="$(bl_get last_preflight_reason)"
  FAIL_TS="$(date -u +%Y%m%dT%H%M%SZ)"
  echo "$FAIL_TS PREFLIGHT FAILED: $PF_REASON — aborting eval, NOT re-triggering chain"
  bl_phase "IDLE" "preflight failed: $PF_REASON"
  {
    echo ""
    echo "## Run — $FAIL_TS (ABORTED)"
    echo "- Preflight FAILED: $PF_REASON"
    echo "- Eval skipped (no quota spent). Chain NOT re-triggered."
    echo "- Fix infra, then: rm -f $STOP && systemctl --user start mqo-buildloop.service"
  } >> "$LEDGER"
  # Do not re-trigger the chain — a broken-infra loop just wastes quota silently.
  exit 1
fi

START_TS="$(date -u +%Y%m%dT%H%M%SZ)"
echo "$START_TS eval start"
bl_phase "EVAL" "k=1 docker-local"

cd "$EVAL_DIR"

GOLD_FILE="$SCRIPT_DIR/../corpus/gold_docker-local.json"
TMPOUT="$(mktemp)"
EVAL_EXIT=0

if [[ -f "$GOLD_FILE" ]]; then
  # Fast path: pre-minted gold, no live oracle needed
  uv run mqo-eval run \
    --corpus corpus/tpcds_sql_derived_limited.yaml \
    --agent claude-oauth \
    --server docker-local \
    --oracle precomputed \
    --gold-file "$GOLD_FILE" \
    --catalog-name "$CATALOG_NAME" \
    --model-name "$MODEL_NAME" \
    --results-dir "$RESULTS_DIR" \
    --repeat 1 2>&1 | tee "$TMPOUT" || EVAL_EXIT=$?
else
  # Fallback: live CLI oracle (shells mqo-pg-query — must be installed in Phase 3)
  # NOTE: --oracle pgwire (psycopg2) is dead against AtScale PGWire and scores 0; never use it.
  echo "WARNING: gold_docker-local.json not found, falling back to cli oracle (mqo-pg-query)"
  uv run mqo-eval run \
    --corpus corpus/tpcds_sql_derived_limited.yaml \
    --agent claude-oauth \
    --server docker-local \
    --oracle cli \
    --catalog-name "$CATALOG_NAME" \
    --model-name "$MODEL_NAME" \
    --results-dir "$RESULTS_DIR" \
    --repeat 1 2>&1 | tee "$TMPOUT" || EVAL_EXIT=$?
fi

END_TS="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ $EVAL_EXIT -ne 0 ]]; then
  echo "$END_TS eval FAILED (exit $EVAL_EXIT)"
  bl_phase "IDLE" "eval harness failed (exit $EVAL_EXIT)"
  rm -f "$TMPOUT"
  exit 1
fi

bl_phase "DIGEST" "scoring + hypotheses"

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

# Haiku digest — analyze failures and write concrete hypotheses inline.
# Never leave this as a placeholder; if claude fails, write the error so it's visible.
FAILING_CASES="$(python3 - "$RECORD_PATH" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
lines = []
for c in data.get('cases', []):
    if c.get('verdict') not in ('correct', 'skipped'):
        j  = c.get('jaccard'); rr = c.get('row_recall'); cj = c.get('column_jaccard')
        d  = (c.get('detail') or '')[:300].replace('\n', ' ')
        ca = c.get('candidate_columns') or []; re = c.get('reference_columns') or []
        lines.append(f"- {c['id']}: j={j} rr={rr} cj={cj}")
        if d:    lines.append(f"  {d}")
        if ca != re: lines.append(f"  cand_cols={ca}  ref_cols={re}")
print('\n'.join(lines) if lines else '(no failures — all passed or skipped)')
PYEOF
)"

HYPOTHESES="$(timeout 90 env -u ANTHROPIC_API_KEY claude -p \
"MQO eval — give terse root-cause hypotheses for each failure, then list top 2 next-DREAM PRD targets.

Pass rate:
$SUMMARY

Failing cases:
$FAILING_CASES

Format (plain text, no markdown, ≤20 lines total):
<case-id>: <one-line root cause>
...
NEXT DREAM: <PRD-filename-stem>: <one-line rationale>" \
  --model claude-haiku-4-5-20251001 \
  --output-format text \
  --max-turns 1 \
  2>/dev/null || echo "(digest failed — manually review: uv run mqo-eval summary --results $RECORD_PATH)")"

# Append ledger entry — hypotheses always filled in, never left pending
{
  echo ""
  echo "## Run — $START_TS → $END_TS"
  echo "- Record: $RECORD_PATH"
  echo "- Gate: k=1"
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
  echo "- Hypotheses:"
  echo "$HYPOTHESES" | sed 's/^/  /'
} >> "$LEDGER"

echo "LEDGER_WRITTEN"
echo "$END_TS ledger appended"

# ── QUOTA GATE ───────────────────────────────────────────────────────────────
# Track score vs best. Pause the chain (don't re-trigger) after NO_IMPROVE_LIMIT
# consecutive evals that fail to set a new best — a plateau is for a human to
# review, not for the loop to grind OAuth quota against.
SCORE="$(echo "$SUMMARY" | grep -oE 'pass rate: [0-9]+/[0-9]+' | sed 's/pass rate: //' | head -1)"
SCORE_PCT="$(echo "$SUMMARY" | grep -oE '\([0-9]+%\)' | tr -dc '0-9' | head -1)"
SCORE_PCT="${SCORE_PCT:-0}"
BEST_PCT="$(bl_get best_score_pct)"; BEST_PCT="${BEST_PCT:-0}"; [[ "$BEST_PCT" =~ ^[0-9]+$ ]] || BEST_PCT=0
NOIMP="$(bl_get no_improvement_count)"; [[ "$NOIMP" =~ ^[0-9]+$ ]] || NOIMP=0
ITER="$(bl_get iteration)"; [[ "$ITER" =~ ^[0-9]+$ ]] || ITER=0

bl_set last_score "\"$SCORE\""
bl_set last_score_pct "$SCORE_PCT"
bl_set iteration "$((ITER + 1))"

if (( SCORE_PCT > BEST_PCT )); then
  bl_set best_score_pct "$SCORE_PCT"
  bl_set no_improvement_count 0
  echo "$END_TS quota-gate: NEW BEST ${SCORE_PCT}% (was ${BEST_PCT}%) — chain continues"
  NOIMP=0
else
  NOIMP=$((NOIMP + 1))
  bl_set no_improvement_count "$NOIMP"
  echo "$END_TS quota-gate: no improvement (${SCORE_PCT}% ≤ best ${BEST_PCT}%) — ${NOIMP}/${NO_IMPROVE_LIMIT}"
fi

bl_phase "IDLE" "score ${SCORE} (${SCORE_PCT}%)"

if (( NOIMP >= NO_IMPROVE_LIMIT )); then
  echo "$END_TS quota-gate: PAUSING chain — ${NOIMP} evals with no new best (best ${BEST_PCT}%)"
  bl_set chain '"paused"'
  touch "$STOP"
  {
    echo "- Quota gate: PAUSED after ${NOIMP} evals with no new best (best ${BEST_PCT}%)."
    echo "  Review, then: rm -f $STOP && bash $SCRIPT_DIR/bl-lib.sh set no_improvement_count 0 && systemctl --user start mqo-buildloop.service"
  } >> "$LEDGER"
  exit 0
fi

# Chain trigger — re-ignite the buildloop service so the next iteration starts immediately.
# The STOP sentinel is checked at service start (ExecCondition), so this is always safe to fire.
# If systemctl is unavailable (e.g. non-systemd env), log and continue — don't abort.
if command -v systemctl &>/dev/null; then
  systemctl --user start mqo-buildloop.service 2>&1 \
    && echo "$END_TS chain: re-triggered mqo-buildloop.service" \
    || echo "$END_TS chain: WARNING — failed to re-trigger mqo-buildloop.service (check systemctl --user status)"
else
  echo "$END_TS chain: systemctl not available — re-ignite manually"
fi
