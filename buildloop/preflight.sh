#!/usr/bin/env bash
# preflight.sh — health gate run BEFORE the eval burns OAuth quota.
#
# Two layers, both must pass:
#   1. DATA  — replay one corpus expected_sql via psql against CE PGWire.
#              Proves CE up + auth + sslmode + the actual data are healthy.
#   2. BINARY — spin the installed mqo-mcp-server with probe enabled and assert
#              the SQL backend status is not a *connection-level* failure
#              (TLS handshake / refused / timeout). Catches the NoTls-regression
#              class directly. A query-level "db error" is OK (eval uses --no-probe).
#
# Exit 0 = healthy (eval may run). Exit 1 = broken (abort eval, do not waste quota).
# Writes last_preflight / last_preflight_reason into state.json.
set -uo pipefail

BL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "$BL_DIR/.." && pwd)"
source "$BL_DIR/bl-lib.sh"

# Load the same env layering the eval uses.
for f in "$HOME/projects/mqo-demo/.env" "$HOME/.config/mcp-watch/secrets.env" "$BL_DIR/docker-local.env"; do
  [[ -f "$f" ]] && { set -a; source "$f"; set +a; }
done

PG_HOST="${MQO_ENDPOINT%%:*}"; PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${MQO_ENDPOINT##*:}"; PG_PORT="${PG_PORT:-15432}"
PG_DBNAME="${ATSCALE_PG_DBNAME:-tpcds_main}"
PG_SSLMODE="${ATSCALE_PG_SSLMODE:-disable}"
PG_USER="${MQO_PG_USER:-admin}"
PG_PASS="${ATSCALE_PG_PASS:-}"
CATALOG_NAME="${DOCKER_CATALOG_NAME:-'"atscale_catalogs"."tpcds_main"'}"
MODEL_NAME="${DOCKER_MODEL_NAME:-'"tpcds_benchmark_model"'}"
CORPUS="$EVAL_DIR/corpus/tpcds_sql_derived_limited.yaml"

fail() {
  echo "PREFLIGHT FAIL: $1" >&2
  bl_set last_preflight '"fail"'
  bl_set last_preflight_reason "$1"
  bl_set last_preflight_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
  exit 1
}
ok() {
  echo "PREFLIGHT PASS: $1"
  bl_set last_preflight '"pass"'
  bl_set last_preflight_reason "$1"
  bl_set last_preflight_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
  exit 0
}

# ── 0. Cheap reachability ────────────────────────────────────────────────────
timeout 5 bash -c "exec 3<>/dev/tcp/$PG_HOST/$PG_PORT" 2>/dev/null \
  || fail "PGWire port $PG_HOST:$PG_PORT not open (CE Docker down?)"

[[ -x "$HOME/.local/bin/mqo-mcp-server" ]] \
  || fail "mqo-mcp-server not installed at ~/.local/bin"

# ── 1. DATA layer: replay one corpus expected_sql via psql ───────────────────
# Pull the first enabled query's expected_sql, substitute catalog/model placeholders.
read -r PROBE_ID PROBE_SQL < <(python3 - "$CORPUS" "$CATALOG_NAME" "$MODEL_NAME" <<'PY'
import sys, yaml, json
corpus, cat, mod = sys.argv[1], sys.argv[2], sys.argv[3]
d = yaml.safe_load(open(corpus))
for q in d["queries"]:
    if q.get("disabled"): continue
    sql = q["expected_sql"].replace("${CatalogName}", cat).replace("${ModelName}", mod)
    sql = " ".join(sql.split())  # one line
    print(q["id"], json.dumps(sql))
    break
PY
)
PROBE_SQL="$(echo "$PROBE_SQL" | python3 -c 'import sys,json; print(json.load(sys.stdin))')"
[[ -n "$PROBE_SQL" ]] || fail "could not extract a probe query from corpus"

DATA_OUT="$(PGPASSWORD="$PG_PASS" timeout 30 psql \
  "host=$PG_HOST port=$PG_PORT dbname=$PG_DBNAME user=$PG_USER sslmode=$PG_SSLMODE" \
  -At -c "$PROBE_SQL" 2>&1)"
DATA_RC=$?
if [[ $DATA_RC -ne 0 ]]; then
  fail "data probe ($PROBE_ID) psql failed: $(echo "$DATA_OUT" | head -1)"
fi
DATA_ROWS="$(echo "$DATA_OUT" | grep -c .)"
[[ "$DATA_ROWS" -ge 1 ]] || fail "data probe ($PROBE_ID) returned 0 rows (data missing?)"

# ── 2. BINARY layer: spin server, classify SQL backend status ────────────────
BANNER="$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"preflight","version":"1"}}}' | \
  timeout 25 "$HOME/.local/bin/mqo-mcp-server" \
    --catalog "$MQO_CATALOG_PATH" \
    --endpoint "$PG_HOST:$PG_PORT" \
    --force-backend sql \
    --pg-user "$PG_USER" \
    --pg-pass-env ATSCALE_PG_PASS \
    2>&1 | grep -i "backends:" | head -1)"

SQL_STATUS="$(echo "$BANNER" | grep -oiE "sql=[^ ]*\([^)]*\)|sql=live" | head -1)"
[[ -n "$SQL_STATUS" ]] || fail "server produced no backend banner (failed to start?)"

# Connection-level failure keywords = broken binary/infra. Query-level = OK (--no-probe in eval).
if echo "$SQL_STATUS" | grep -qiE "tls|handshake|refused|timed out|timeout|connection (error|reset|closed)|no route|unreachable host"; then
  fail "server SQL backend connection broken: $SQL_STATUS"
fi

ok "data($PROBE_ID:$DATA_ROWS rows) binary($SQL_STATUS)"
