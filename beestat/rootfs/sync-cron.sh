#!/usr/bin/env bash
# Periodic sync — the piece beestat.io runs as a server cron and a self-hosted
# install otherwise lacks. Without it beestat only syncs while its page is open,
# so the initial (up to a year) backfill never finishes -- and until it does,
# sync_forwards() never runs, so current data is never appended and the header /
# graphs go stale (axis advances, data doesn't).
#
# beestat's sync methods are private, so we authenticate as the logged-in user
# with their session (session->touch keeps it alive on each call) plus the local
# API key. bypass_cache_read=1 defeats the 5-minute sync cache so every tick does
# real work (advances the backfill, then keeps forward sync current). Entirely
# server-side; no changes to the beestat app itself.
set -uo pipefail

SOCK=/run/mysqld/mysqld.sock
BASE="http://127.0.0.1:8128/api/"
INTERVAL="${SYNC_INTERVAL:-300}"

log() { echo "[beestat-sync] $*"; }

# shellcheck disable=SC1091
source /data/secrets.env 2>/dev/null || true

# $1 resource, $2 method, $3 session_key -> prints the HTTP status code. If
# beestat returns a failure envelope (e.g. a backfill chunk erroring, which sync
# swallows silently), log a snippet so a stall is visible instead of invisible.
call() {
  local out http body
  out="$(curl -s -w $'\n%{http_code}' --max-time 3600 -G \
    --data-urlencode "api_key=${BEESTAT_API_KEY:-}" \
    --data-urlencode "resource=$1" \
    --data-urlencode "method=$2" \
    --data-urlencode "arguments={}" \
    --data-urlencode "bypass_cache_read=1" \
    -H "Cookie: session_key=$3" \
    "${BASE}" 2>/dev/null)" || { echo "000"; return; }
  http="${out##*$'\n'}"
  body="${out%$'\n'*}"
  case "$body" in
    *'"success":false'*) log "$1->$2 error: $(printf '%s' "$body" | head -c 300)" ;;
  esac
  echo "${http:-000}"
}

# Temperature profiles (Analyze tab) are built by a scheduled job, not computed
# live, and beestat treats them as a WEEKLY dataset (the GUI generate_profile is
# cached 7 days). Regenerate only when the marker is missing or older than ~7
# days, to match that cadence. generate_profiles bypasses the per-thermostat
# cache, so this also clears a stale/empty profile left cached from a bad-data
# window. The marker lives in /data (survives restarts) so restarting the add-on
# does not force off-cadence regeneration.
PROFILE_MARKER=/data/.profiles-generated

log "started; interval=${INTERVAL}s"
# Let nginx/php-fpm/MySQL settle before the first tick.
sleep 30

while true; do
  session_key="$(mysql --socket="${SOCK}" -u root -N -e \
    "SELECT session_key FROM beestat.session WHERE deleted=0 AND user_id IS NOT NULL ORDER BY last_used_at DESC, session_id DESC LIMIT 1" \
    2>/dev/null || true)"

  if [ -z "${session_key}" ]; then
    log "no logged-in session yet; open beestat once so it logs in, then this takes over"
  else
    # Same order the frontend uses on load: thermostat + sensors first, then
    # runtime (which needs the thermostats to exist). Separate calls so one
    # failing does not block the others.
    t="$(call thermostat sync "${session_key}")"
    s="$(call sensor sync "${session_key}")"
    r="$(call runtime sync "${session_key}")"
    log "sync http: thermostat=${t} sensor=${s} runtime=${r}"

    if [ ! -f "${PROFILE_MARKER}" ] || [ -n "$(find "${PROFILE_MARKER}" -mtime +6 2>/dev/null)" ]; then
      p="$(call thermostat generate_profiles "${session_key}")"
      log "generate_profiles http=${p} (weekly)"
      [ "${p}" = "200" ] && touch "${PROFILE_MARKER}"
    fi
  fi

  sleep "${INTERVAL}"
done
