# Changelog

## 0.5.5

- Generate temperature profiles on a WEEKLY cadence (marker-based, survives
  restarts) to match beestat's documented behavior, instead of daily. Still
  regenerates immediately on first run to clear a stale/empty profile.

## 0.5.4

- Generate temperature profiles (Analyze tab). beestat builds these with a
  scheduled job, not live; without it the Analyze tab shows "No data to display"
  even with runtime synced. The background sync now calls
  thermostat->generate_profiles on the first cycle and ~daily thereafter, as
  beestat.io's cron does.

## 0.5.3

- Raise php-fpm workers (pm.max_children 5 -> 12). Long-running backfill syncs
  hold a worker for minutes at a time, so with the default of 5 the pool
  saturated ("server reached pm.max_children") and other requests queued/stalled
  — a thermostat could get stuck on "Syncing". More workers keeps the app
  responsive during backfill.
- Background sync now logs the error body when a sync returns a failure envelope,
  so a silently-swallowed backfill stall is visible in the log.

## 0.5.2

- Fix timezone-shifted graph data. beestat stores timestamps as UTC and reads
  them back assuming a UTC database; rows written before MySQL was pinned to UTC
  (MariaDB era / pre-UTC builds) were stored under the container's local timezone
  and read back shifted by the UTC offset, so data no longer lined up with the
  time axis. One-time reset of the derived runtime tables + sync markers so the
  background sync repopulates them under UTC.
- Load MySQL's named time zone tables (mysql_tzinfo_to_sql) so CONVERT_TZ with
  named zones works, per beestat's self-hosting docs.

## 0.5.1

- Let the initial history backfill actually finish. beestat syncs up to a year of
  runtime in a single long request; it was dying on the 256M PHP memory limit
  (OOM 500s) and nginx's 120s read timeout (504s), so it only crawled forward.
  Raise PHP memory_limit to 1024M and max_execution_time, nginx
  fastcgi_read_timeout, and the sync client timeout to 3600s. This is a one-time
  cost during backfill; steady-state forward syncs are light.

## 0.5.0

- Add a background sync (the cron beestat.io runs but a self-hosted install
  lacks). Every 5 minutes it calls thermostat/sensor/runtime sync as the
  logged-in user, so the initial year-long backfill actually completes and the
  header, sensors, and graphs stay current without keeping the beestat page
  open. Interval configurable via SYNC_INTERVAL; server-side only, no app
  changes. Log lines are prefixed [beestat-sync].

## 0.4.0

- Pin PHP 8.1 (the version upstream beestat targets) and drop the cora
  deprecation patch from the fork. Keeps the fork minimal and byte-for-byte
  mergeable with upstream: the only fork changes now are the ones genuinely
  required for self-hosting (configurable API base URL, HTTP/port-safe redirect
  and cookies, optional IPv4). 8.1 is EOL for security patches but this is a
  LAN-only app behind Home Assistant; revisit when upstream moves to 8.2+.

## 0.3.3

- Pin MySQL to UTC (--default-time-zone=+00:00). Home Assistant injects the
  host's local timezone into add-on containers, so MySQL came up in a
  DST-observing zone; beestat stores UTC timestamps, and a UTC value in a
  spring-forward gap (e.g. 02:00 on a US DST day) was rejected with "Incorrect
  datetime value". Matches how the hosted beestat runs.

## 0.3.2

- Fix a regression on PHP 8.3 where dynamic-property deprecation notices were
  re-caught by cora's shutdown handler and returned to the app as an error
  ("won't load" after a deprecation message). Deprecations are now fully
  suppressed and never become error responses.

## 0.3.1

- Force beestat's server-to-server ecobee/bridge cURL calls over IPv4
  (external_api_ipv4_only). Inside the container the bridge's mDNS hostname could
  resolve to an unreachable IPv6 address, so the token exchange failed with
  "connection reset by peer". This removes the need to hardcode the host's IP in
  bridge_url — the hostname now works.

## 0.3.0

- Use current PHP 8.3 instead of the end-of-life 8.1 pin. The fork now patches
  cora so deprecation notices (e.g. dynamic-property creation, deprecated in PHP
  8.2) are logged and non-fatal rather than crashing the request, so beestat runs
  on modern, supported PHP and survives future upstream/PHP updates.

## 0.2.2

- Use PHP 8.1 (was 8.2). cora dies on any error, and creating a dynamic property
  (which cora does, e.g. request::$total_time) is deprecated in PHP 8.2+, which
  made the shutdown handler die before emitting the response -> blank 200 on
  every API call. PHP 8.1 matches what upstream targets.

## 0.2.1

- Run php-fpm in the foreground so PHP fatals surface in the add-on Log tab
  instead of being swallowed by a daemonized master.

## 0.2.0

- Bundle genuine MySQL 8 (Percona Server 8.0) instead of MariaDB. cora detects
  JSON columns using MySQL-8 semantics in both directions; MariaDB reports them
  as longtext, which silently broke every `/api/` call. Existing MariaDB data
  directories are automatically reinitialized on first boot.
- Drop the MariaDB collation remap (utf8mb4_0900_ai_ci is now native).
- Log PHP errors to the add-on Log tab.

## 0.1.x

- Initial beestat web-app add-on: PHP + nginx + database, built from the
  companion fork, pointed at the Beestat Bridge instead of the ecobee cloud API.
