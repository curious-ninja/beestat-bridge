# Changelog

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
