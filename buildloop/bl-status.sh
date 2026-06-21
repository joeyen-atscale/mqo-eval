#!/usr/bin/env bash
# bl-status.sh — one-screen buildloop status. No pstree/proc spelunking.
# Reads state.json (source of truth) + augments with live signals.
set -uo pipefail

# Resolve through symlinks so `buildloop-status` (a symlink in ~/.local/bin) finds
# the real buildloop directory, not its own dir.
_src="${BASH_SOURCE[0]}"; while [[ -L "$_src" ]]; do _src="$(readlink "$_src")"; done
BL_DIR="$(cd "$(dirname "$_src")" && pwd)"
STATE="$BL_DIR/state.json"
LEDGER="$BL_DIR/ledger.md"
EVAL_LOG="$BL_DIR/eval.log"
LOCK="$BL_DIR/.eval.lock"
STOP="$BL_DIR/STOP"
SESSION_LOCK="$HOME/.config/wm-burst/session.lock"

c_grn=$'\033[32m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

# ── Service state ────────────────────────────────────────────────────────────
SVC="$(systemctl --user is-active mqo-buildloop.service 2>/dev/null)"; SVC="${SVC:-unknown}"
SINCE="$(systemctl --user show mqo-buildloop.service -p ActiveEnterTimestamp --value 2>/dev/null)"

echo "── mqo-buildloop status ──────────────────────────────────────"
case "$SVC" in
  active|activating) echo "  service:    ${c_grn}${SVC}${c_off}  (since ${SINCE:-?})" ;;
  *)                 echo "  service:    ${c_dim}${SVC}${c_off}" ;;
esac

# ── STOP / chain ─────────────────────────────────────────────────────────────
if [[ -e "$STOP" ]]; then
  echo "  chain:      ${c_red}STOPPED${c_off}  (remove $STOP to resume)"
else
  echo "  chain:      ${c_grn}armed${c_off}"
fi

# ── state.json ───────────────────────────────────────────────────────────────
if [[ -f "$STATE" ]] && command -v jq >/dev/null; then
  phase="$(jq -r '.phase // "?"' "$STATE")"
  pdetail="$(jq -r '.phase_detail // ""' "$STATE")"
  pstart="$(jq -r '.phase_started // ""' "$STATE")"
  iter="$(jq -r '.iteration // 0' "$STATE")"
  last="$(jq -r '.last_score // "—"' "$STATE")"
  lastpct="$(jq -r '.last_score_pct // "—"' "$STATE")"
  best="$(jq -r '.best_score_pct // "—"' "$STATE")"
  noimp="$(jq -r '.no_improvement_count // 0' "$STATE")"
  pf="$(jq -r '.last_preflight // "—"' "$STATE")"
  pfr="$(jq -r '.last_preflight_reason // ""' "$STATE")"
  pfat="$(jq -r '.last_preflight_at // ""' "$STATE")"

  # elapsed in current phase
  el=""
  if [[ -n "$pstart" && "$pstart" != "null" ]]; then
    s=$(date -u -d "$pstart" +%s 2>/dev/null || echo 0)
    n=$(date -u +%s)
    [[ "$s" -gt 0 ]] && el="$(( (n-s)/60 ))m $(( (n-s)%60 ))s"
  fi

  echo "  iteration:  #$iter"
  echo "  phase:      ${c_yel}${phase}${c_off}${el:+  (${el})}${pdetail:+  — $pdetail}"
  case "$pf" in
    pass) echo "  preflight:  ${c_grn}pass${c_off}  ${c_dim}${pfr}${c_off}" ;;
    fail) echo "  preflight:  ${c_red}FAIL${c_off}  ${pfr}  ${c_dim}${pfat}${c_off}" ;;
    *)    echo "  preflight:  ${c_dim}—${c_off}" ;;
  esac
  echo "  last score: ${last} (${lastpct}%)   best: ${best}%   no-improve: ${noimp}"
else
  echo "  state:      ${c_dim}(no state.json yet)${c_off}"
fi

# ── Live signals ─────────────────────────────────────────────────────────────
if command -v flock >/dev/null && [[ -f "$LOCK" ]]; then
  if ! ( exec 9>"$LOCK"; flock -n 9 ) 2>/dev/null; then
    echo "  eval:       ${c_grn}RUNNING${c_off}  (lock held)"
  fi
fi
if [[ -f "$SESSION_LOCK" ]]; then
  ip="$(jq -r '.ip // "?"' "$SESSION_LOCK" 2>/dev/null)"
  echo "  cloudbuild: ${c_yel}session active${c_off}  ($ip — billing)"
fi

# ── Last ledger entry ────────────────────────────────────────────────────────
if [[ -f "$LEDGER" ]]; then
  echo "── last ledger entry ─────────────────────────────────────────"
  awk '/^## Run —/{p=NR} END{print p}' "$LEDGER" >/dev/null
  tac "$LEDGER" | awk '/^## Run —/{print; exit} {buf=$0"\n"prev; prev=buf} END{}' >/dev/null 2>&1
  # Simple: print from the last "## Run —" header to EOF, capped.
  last_hdr="$(grep -n '^## Run —' "$LEDGER" | tail -1 | cut -d: -f1)"
  if [[ -n "$last_hdr" ]]; then
    tail -n +"$last_hdr" "$LEDGER" | head -30
  fi
fi
