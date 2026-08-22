#!/bin/bash
# Рендер хостового конфига nginx из .env.
#
# envsubst вызывается со списком переменных не случайно: без него он съел бы
# собственные переменные nginx ($host, $remote_addr и прочие), и конфиг стал бы
# бессмысленным. Подставляются ровно те, что перечислены.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/relay/app}
TARGET=${TARGET:-/etc/nginx/sites-available/relay}

set -a
# shellcheck disable=SC1091
. "$APP_DIR/.env"
set +a

: "${MAIN_SERVER_IP:?MAIN_SERVER_IP must be set in .env}"
: "${RELAY_SERVER_NAME:=relay.internal}"
: "${RELAY_TLS_PORT:=8443}"
: "${RELAY_PORT:=8081}"
export MAIN_SERVER_IP RELAY_SERVER_NAME RELAY_TLS_PORT RELAY_PORT

envsubst '${MAIN_SERVER_IP} ${RELAY_SERVER_NAME} ${RELAY_TLS_PORT} ${RELAY_PORT}' \
    < "$APP_DIR/deploy/nginx-relay.conf.template" > "$TARGET"
chmod 644 "$TARGET"

nginx -t
echo "nginx config rendered to $TARGET"
