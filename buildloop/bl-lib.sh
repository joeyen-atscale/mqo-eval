#!/usr/bin/env bash
# bl-lib.sh — shared state for the mqo-buildloop operational layer.
#
# Single source of truth: state.json. Every phase stamps its progress here so
# `bl-status.sh` can report cleanly instead of spelunking pstree/proc/lsof.
#
# Use as a library:   source bl-lib.sh
# Use as a CLI:       bash bl-lib.sh phase EVAL "k=1 docker-local"
#                     bash bl-lib.sh set last_score_pct 70
#                     bash bl-lib.sh get phase
set -uo pipefail

BL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$BL_DIR/state.json"

# Initialise an empty state file if missing.
_bl_init() {
  [[ -f "$STATE" ]] && return 0
  cat > "$STATE" <<'JSON'
{
  "iteration": 0,
  "phase": "IDLE",
  "phase_detail": "",
  "phase_started": null,
  "updated": null,
  "last_score": null,
  "last_score_pct": null,
  "best_score_pct": null,
  "no_improvement_count": 0,
  "last_preflight": null,
  "last_preflight_reason": "",
  "last_preflight_at": null,
  "chain": "armed"
}
JSON
}

_bl_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# bl_set <key> <value-as-json-or-string>  — strings are auto-quoted unless valid JSON.
bl_set() {
  _bl_init
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  # If val parses as JSON (number, bool, null, object), use raw; else treat as string.
  if echo "$val" | jq -e . >/dev/null 2>&1; then
    jq --arg k "$key" --argjson v "$val" '.[$k]=$v | .updated=(now|todateiso8601)' "$STATE" > "$tmp" 2>/dev/null \
      || jq --arg k "$key" --arg v "$val" '.[$k]=$v' "$STATE" > "$tmp"
  else
    jq --arg k "$key" --arg v "$val" '.[$k]=$v' "$STATE" > "$tmp"
  fi
  # Stamp updated separately (now|todateiso8601 not always available) and commit.
  jq --arg u "$(_bl_now)" '.updated=$u' "$tmp" > "$STATE" 2>/dev/null || mv "$tmp" "$STATE"
  rm -f "$tmp"
}

bl_get() { _bl_init; jq -r --arg k "$1" '.[$k] // ""' "$STATE" 2>/dev/null; }

# bl_phase <NAME> [detail] — stamp a phase transition.
bl_phase() {
  _bl_init
  local name="$1" detail="${2:-}" tmp
  tmp="$(mktemp)"
  jq --arg p "$name" --arg d "$detail" --arg t "$(_bl_now)" \
    '.phase=$p | .phase_detail=$d | .phase_started=$t | .updated=$t' \
    "$STATE" > "$tmp" && mv "$tmp" "$STATE" || rm -f "$tmp"
}

# CLI dispatch when run directly.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"; shift || true
  case "$cmd" in
    phase) bl_phase "$@" ;;
    set)   bl_set "$@" ;;
    get)   bl_get "$@" ;;
    init)  _bl_init ;;
    *) echo "usage: bl-lib.sh {phase <NAME> [detail]|set <k> <v>|get <k>|init}" >&2; exit 2 ;;
  esac
fi
