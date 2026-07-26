# Changelog

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
