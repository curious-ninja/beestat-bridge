#!/usr/bin/env bash
# Entrypoint for the beestat add-on: render config, bring up MySQL (first-run
# init + schema import), then php-fpm and nginx. Idempotent across restarts.
set -euo pipefail

WWW=/var/www/html
DATA=/data
DB_DATA="${DATA}/mysql"
SECRETS="${DATA}/secrets.env"
INIT_MARKER="${DATA}/.db-initialized"
OPTIONS=/data/options.json
SOCK=/run/mysqld/mysqld.sock

log() { echo "[beestat] $*"; }

# --- options (HA options.json, else environment) ---------------------------
BRIDGE_URL="$(jq -r '.bridge_url // empty' "${OPTIONS}" 2>/dev/null || true)"
APP_URL="$(jq -r '.app_url // empty' "${OPTIONS}" 2>/dev/null || true)"
BRIDGE_URL="${BRIDGE_URL:-${BRIDGE_URL_ENV:-http://homeassistant.local:8127}}"
APP_URL="${APP_URL:-${APP_URL_ENV:-http://homeassistant.local:8128}}"
BRIDGE_URL="${BRIDGE_URL%/}"
APP_URL="${APP_URL%/}"
log "bridge_url=${BRIDGE_URL} app_url=${APP_URL}"

# --- persistent secrets (API keys + DB password), generated once -----------
if [ ! -f "${SECRETS}" ]; then
  {
    echo "BEESTAT_API_KEY=$(openssl rand -hex 20)"
    echo "ECOBEE_API_KEY=$(openssl rand -hex 20)"
    echo "DB_PASSWORD=$(openssl rand -hex 16)"
  } > "${SECRETS}"
  log "generated secrets"
fi
# shellcheck disable=SC1090
source "${SECRETS}"

# --- render api/cora/setting.php from the shipped example ------------------
cp "${WWW}/api/cora/setting.example.php" "${WWW}/api/cora/setting.php"
sed -i \
  -e "s|'beestat_api_key_local' => ''|'beestat_api_key_local' => '${BEESTAT_API_KEY}'|" \
  -e "s|'ecobee_api_key_local' => ''|'ecobee_api_key_local' => '${ECOBEE_API_KEY}'|" \
  -e "s|'ecobee_redirect_uri' => ''|'ecobee_redirect_uri' => '${APP_URL}/api/ecobee_initialize.php'|" \
  -e "s|'beestat_root_uri' => ''|'beestat_root_uri' => '${APP_URL}/'|" \
  -e "s|'ecobee_api_base_url' => 'https://api.ecobee.com'|'ecobee_api_base_url' => '${BRIDGE_URL}'|" \
  -e "s|'external_api_ipv4_only' => false|'external_api_ipv4_only' => true|" \
  -e "s|'database_host' => ''|'database_host' => '127.0.0.1'|" \
  -e "s|'database_username' => ''|'database_username' => 'beestat'|" \
  -e "s|'database_password' => ''|'database_password' => '${DB_PASSWORD}'|" \
  -e "s|'database_name' => ''|'database_name' => 'beestat'|" \
  -e "s|'force_ssl' => true|'force_ssl' => false|" \
  "${WWW}/api/cora/setting.php"
log "wrote setting.php (environment=dev, no JS build needed)"

# --- MySQL 8 (Percona Server) ----------------------------------------------
mkdir -p "${DB_DATA}" /run/mysqld
chown -R mysql:mysql "${DB_DATA}" /run/mysqld 2>/dev/null || true

ENGINE_STAMP="${DB_DATA}/.engine"
WANT_ENGINE="percona-mysql8"

# A data directory created by a different engine (e.g. a prior MariaDB build of
# this add-on) is incompatible with MySQL 8 -- mysqld will refuse to start on
# it. There is no valuable data before the first successful ecobee login, so if
# the datadir is foreign, wipe and re-init rather than fail to boot.
if [ -d "${DB_DATA}/mysql" ] || [ -f "${DB_DATA}/mysql.ibd" ]; then
  if [ "$(cat "${ENGINE_STAMP}" 2>/dev/null || true)" != "${WANT_ENGINE}" ]; then
    log "existing data directory is from a different database engine; reinitializing"
    find "${DB_DATA}" -mindepth 1 -delete 2>/dev/null || true
    rm -f "${INIT_MARKER}"
  fi
fi

if [ ! -f "${ENGINE_STAMP}" ]; then
  log "initializing MySQL data directory"
  find "${DB_DATA}" -mindepth 1 -delete 2>/dev/null || true
  mysqld --initialize-insecure --user=mysql --datadir="${DB_DATA}"
  echo "${WANT_ENGINE}" > "${ENGINE_STAMP}"
  chown mysql:mysql "${ENGINE_STAMP}" 2>/dev/null || true
fi

log "starting MySQL"
# Pin the server to UTC regardless of the container's inherited TZ (Home
# Assistant injects the host's local timezone into add-on containers). beestat
# stores all timestamps as UTC and assumes the database is UTC; if MySQL runs in
# a DST-observing zone, UTC values that land in a spring-forward gap (e.g.
# 02:00 on a US DST day) are rejected with "Incorrect datetime value".
mysqld --user=mysql --datadir="${DB_DATA}" \
  --socket="${SOCK}" --bind-address=127.0.0.1 --port=3306 --mysqlx=OFF \
  --default-time-zone='+00:00' &
DB_PID=$!

# Flush MySQL cleanly when the add-on is stopped.
shutdown() {
  log "stopping"
  nginx -s quit 2>/dev/null || true
  mysqladmin --socket="${SOCK}" -u root shutdown 2>/dev/null || true
  wait "${DB_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown SIGTERM SIGINT

for _ in $(seq 1 60); do
  if mysqladmin --socket="${SOCK}" ping >/dev/null 2>&1; then break; fi
  sleep 1
done
mysqladmin --socket="${SOCK}" ping >/dev/null 2>&1 \
  || { log "MySQL failed to start"; exit 1; }

# root@localhost has no password after --initialize-insecure and connects over
# the local socket. Create the app database and user (mysql_native_password so
# PHP's mysqli authenticates over TCP without caching_sha2 negotiation quirks).
mysql --socket="${SOCK}" -u root <<SQL
CREATE DATABASE IF NOT EXISTS beestat CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'beestat'@'127.0.0.1' IDENTIFIED WITH mysql_native_password BY '${DB_PASSWORD}';
ALTER USER 'beestat'@'127.0.0.1' IDENTIFIED WITH mysql_native_password BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON beestat.* TO 'beestat'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

if [ ! -f "${INIT_MARKER}" ]; then
  log "importing schema"
  # beestat.sql is a genuine MySQL 8 dump, so on Percona/MySQL 8 the
  # utf8mb4_0900_ai_ci collation and all json columns import natively -- no
  # collation remapping needed. The one fix required is an upstream quirk: a
  # trailing comma before ") ENGINE" in the api_user table (a syntax error on
  # any engine). Done in the stream so the fork's copy is untouched.
  perl -0777 -pe 's/,(\s*\n\s*\)\s*ENGINE)/$1/g' "${WWW}/api/beestat.sql" \
    | mysql --socket="${SOCK}" -u root beestat
  # Seed the two API users beestat expects (frontend + ecobee-callback keys).
  mysql --socket="${SOCK}" -u root beestat <<SQL
INSERT INTO api_user (api_user_id, name, api_key) VALUES
  (1, 'beestat_local', '${BEESTAT_API_KEY}'),
  (2, 'ecobee_local', '${ECOBEE_API_KEY}')
ON DUPLICATE KEY UPDATE api_key = VALUES(api_key);
SQL
  touch "${INIT_MARKER}"
  log "schema imported and API users seeded"
fi

# --- PHP-FPM + nginx -------------------------------------------------------
# Run php-fpm in the FOREGROUND (backgrounded with &) rather than daemonized
# (-D): daemonizing detaches worker stderr, which swallows PHP fatals. Keeping
# it attached routes worker output (catch_workers_output) to this script's
# stderr, i.e. the add-on Log tab, so any uncaught error is visible.
log "starting php-fpm"
php-fpm --nodaemonize --force-stderr &
FPM_PID=$!
log "starting nginx on :8128"
nginx &
NGINX_PID=$!
# Wait on any service; trap handles clean shutdown.
wait -n "${DB_PID}" "${FPM_PID}" "${NGINX_PID}"
