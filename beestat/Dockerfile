# Base image pinned (not HA's BUILD_FROM) so the Debian/apt toolchain and the
# bundled MariaDB/nginx/PHP are predictable across arches.
FROM php:8.2-fpm-bookworm

# The beestat application source is pulled from the companion fork at build
# time (kept out of this repo so the fork stays independently updatable). Both
# default to this project's public repos; override to build a different fork.
ARG BEESTAT_REPO=https://github.com/curious-ninja/beestat-app
ARG BEESTAT_REF=claude/beestat-home-assistant-m4qbve

RUN apt-get update && apt-get install -y --no-install-recommends \
      nginx mariadb-server git ca-certificates jq \
    && docker-php-ext-install mysqli \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# The php base image already ships /var/www/html (non-empty), so clone to a
# temp dir and copy the contents into the existing web root.
RUN git clone --depth 1 --branch "${BEESTAT_REF}" "${BEESTAT_REPO}" /tmp/beestat \
    && cp -a /tmp/beestat/. /var/www/html/ \
    && rm -rf /tmp/beestat /var/www/html/.git

COPY rootfs/nginx.conf /etc/nginx/nginx.conf
COPY rootfs/run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8128
CMD ["/run.sh"]
