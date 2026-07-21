#!/usr/bin/env bash
# Entrypoint for the beestat add-on: render config, bring up MariaDB (first-run
# init + schema import), then php-fpm and nginx. Idempotent across restarts.
set -euo pipefail

WWW=/var/www/html
DATA=/data
DB_DATA="${DATA}/mysql"
SECRETS="${DATA}/secrets.env"
INIT_MARKER="${DATA}/.db-initialized"
OPTIONS=/data/options.json

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
  -e "s|'database_host' => ''|'database_host' => '127.0.0.1'|" \
  -e "s|'database_username' => ''|'database_username' => 'beestat'|" \
  -e "s|'database_password' => ''|'database_password' => '${DB_PASSWORD}'|" \
  -e "s|'database_name' => ''|'database_name' => 'beestat'|" \
  -e "s|'force_ssl' => true|'force_ssl' => false|" \
  "${WWW}/api/cora/setting.php"
log "wrote setting.php (environment=dev, no JS build needed)"

# --- MariaDB ---------------------------------------------------------------
mkdir -p "${DB_DATA}" /run/mysqld
chown -R mysql:mysql "${DB_DATA}" /run/mysqld 2>/dev/null || true

if [ ! -d "${DB_DATA}/mysql" ]; then
  log "initializing MariaDB data directory"
  mariadb-install-db --user=mysql --datadir="${DB_DATA}" \
    --auth-root-authentication-method=normal --skip-test-db >/dev/null
fi

log "starting MariaDB"
mariadbd --user=mysql --datadir="${DB_DATA}" \
  --socket=/run/mysqld/mysqld.sock --bind-address=127.0.0.1 --port=3306 &
DB_PID=$!

# Flush MariaDB cleanly when the add-on is stopped.
shutdown() {
  log "stopping"
  nginx -s quit 2>/dev/null || true
  mariadb-admin --socket=/run/mysqld/mysqld.sock shutdown 2>/dev/null || true
  wait "${DB_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown SIGTERM SIGINT

for _ in $(seq 1 60); do
  if mariadb-admin --socket=/run/mysqld/mysqld.sock ping >/dev/null 2>&1; then break; fi
  sleep 1
done
mariadb-admin --socket=/run/mysqld/mysqld.sock ping >/dev/null 2>&1 \
  || { log "MariaDB failed to start"; exit 1; }

mariadb --socket=/run/mysqld/mysqld.sock <<SQL
CREATE DATABASE IF NOT EXISTS beestat CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'beestat'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER 'beestat'@'127.0.0.1' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON beestat.* TO 'beestat'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

if [ ! -f "${INIT_MARKER}" ]; then
  log "importing schema (sanitizing upstream trailing-comma in api_user)"
  # Upstream beestat.sql has a trailing comma before ) ENGINE in one table,
  # which is a MySQL syntax error; strip it without touching the fork's copy.
  perl -0777 -pe 's/,(\s*\n\s*\)\s*ENGINE)/$1/g' "${WWW}/api/beestat.sql" \
    | mariadb --socket=/run/mysqld/mysqld.sock beestat
  # Seed the two API users beestat expects (frontend + ecobee-callback keys).
  mariadb --socket=/run/mysqld/mysqld.sock beestat <<SQL
INSERT INTO api_user (api_user_id, name, api_key) VALUES
  (1, 'beestat_local', '${BEESTAT_API_KEY}'),
  (2, 'ecobee_local', '${ECOBEE_API_KEY}')
ON DUPLICATE KEY UPDATE api_key = VALUES(api_key);
SQL
  touch "${INIT_MARKER}"
  log "schema imported and API users seeded"
fi

# --- PHP-FPM + nginx -------------------------------------------------------
log "starting php-fpm"
php-fpm -D
log "starting nginx on :8128"
nginx &
NGINX_PID=$!
# Wait on either service; trap handles clean shutdown.
wait -n "${DB_PID}" "${NGINX_PID}"
