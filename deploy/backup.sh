#!/bin/bash
# Суточный дамп SQLite релея. Запускается systemd-таймером на VPS.
#
# Копировать файл БД на ходу нельзя: включён WAL, и обычный cp даёт
# полудамп. Поэтому используется sqlite3 backup API изнутри контейнера —
# он снимает согласованный снимок под своей блокировкой.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/relay/app}
BACKUP_DIR=${BACKUP_DIR:-/opt/relay/backups}
RETENTION=${RETENTION:-14}

compose() {
    docker compose --env-file "$APP_DIR/.env" -f "$APP_DIR/compose.yaml" "$@"
}

stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="/backups/relay-$stamp.sqlite3"

compose exec -T relay python - "$target" <<'PY'
import sqlite3
import sys

target = sys.argv[1]
source = sqlite3.connect("/data/relay.sqlite3")
destination = sqlite3.connect(target)
with destination:
    source.backup(destination)
destination.close()
source.close()
PY

echo "backup written: $BACKUP_DIR/relay-$stamp.sqlite3"

# Оставляем $RETENTION свежих копий, остальные удаляем.
ls -1t "$BACKUP_DIR"/relay-*.sqlite3 2>/dev/null \
    | tail -n +$((RETENTION + 1)) \
    | while read -r old; do rm -f "$old" && echo "backup pruned: $old"; done
