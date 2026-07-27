# Base image pinned (not HA's BUILD_FROM) so the Debian/apt toolchain, PHP, and
# nginx are predictable across arches. PHP 8.1 matches the version the upstream
# hosted beestat targets. This matters because cora runs with error_reporting(-1)
# and an error handler that treats ANY notice as fatal; cora relies on dynamic
# properties (e.g. request::$total_time), and creating one became E_DEPRECATED in
# PHP 8.2. Pinning 8.1 keeps the fork byte-for-byte identical to upstream (no cora
# patch needed). Revisit when upstream itself moves to 8.2+.
FROM php:8.1-fpm-bookworm

# The beestat application source is pulled from the companion fork at build
# time (kept out of this repo so the fork stays independently updatable). Both
# default to this project's public repos; override to build a different fork.
ARG BEESTAT_REPO=https://github.com/curious-ninja/beestat-app
ARG BEESTAT_REF=claude/beestat-home-assistant-m4qbve

ENV DEBIAN_FRONTEND=noninteractive

# Genuine MySQL 8 (via Percona Server 8.0), NOT MariaDB. cora detects JSON
# columns using MySQL-8 semantics in BOTH directions -- writes check
# information_schema DATA_TYPE = 'json', reads check the mysqli field type 245.
# MariaDB reports JSON columns as longtext, so cora neither json_encodes on write
# (real_escape_string(array) -> TypeError -> blank 200) nor json_decodes on read
# (the "converged" column comes back as a raw string). Percona Server is a
# drop-in MySQL 8.0 with identical JSON/collation behavior and ships arm64 +
# amd64 packages for bookworm, so the app stays byte-for-byte identical to
# upstream (no cora patches) and thus mergeable.
#
# policy-rc.d stops the package postinst from trying to start the server during
# the build (there is no init system in a build layer).
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update && apt-get install -y --no-install-recommends \
      curl gnupg ca-certificates \
    && curl -fsSL -o /tmp/percona-release.deb \
         https://repo.percona.com/apt/percona-release_latest.generic_all.deb \
    && apt-get install -y --no-install-recommends /tmp/percona-release.deb \
    && percona-release setup -y ps80 \
    && apt-get update && apt-get install -y --no-install-recommends \
      percona-server-server percona-server-client \
      nginx git jq \
    && docker-php-ext-install mysqli \
    && rm -f /tmp/percona-release.deb \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# PHP: surface fatals/warnings in the add-on Log tab (stderr) instead of a blank
# 200, and give the app sane limits. display_errors stays Off so JSON API
# responses are never corrupted by inline error output. Also disable
# zlib.output_compression -- the app already gzips via ob_gzhandler, and layering
# a second compressor produced confusing empty-gzip bodies while debugging.
RUN { \
      echo 'display_errors = Off'; \
      echo 'log_errors = On'; \
      echo 'error_log = /proc/self/fd/2'; \
      echo 'error_reporting = E_ALL'; \
      echo 'zlib.output_compression = Off'; \
      echo 'max_execution_time = 3600'; \
      echo 'memory_limit = 1024M'; \
    } > /usr/local/etc/php/conf.d/zz-beestat.ini \
    && { \
      echo '[www]'; \
      echo 'catch_workers_output = yes'; \
      echo 'decorate_workers_output = no'; \
    } > /usr/local/etc/php-fpm.d/zz-beestat.conf

# The php base image already ships /var/www/html (non-empty), so clone to a
# temp dir and copy the contents into the existing web root. BUILD_VERSION
# (passed by the Supervisor, = the add-on version) is referenced here so that
# bumping the add-on version busts this layer's cache and re-clones the fork,
# picking up new fork commits.
ARG BUILD_VERSION=dev
RUN echo "beestat add-on ${BUILD_VERSION}" \
    && git clone --depth 1 --branch "${BEESTAT_REF}" "${BEESTAT_REPO}" /tmp/beestat \
    && cp -a /tmp/beestat/. /var/www/html/ \
    && rm -rf /tmp/beestat /var/www/html/.git

COPY rootfs/nginx.conf /etc/nginx/nginx.conf
COPY rootfs/run.sh /run.sh
COPY rootfs/sync-cron.sh /sync-cron.sh
RUN chmod +x /run.sh /sync-cron.sh

EXPOSE 8128
CMD ["/run.sh"]
