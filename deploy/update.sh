#!/usr/bin/env bash
# Автообновление: если в основной ветке GitHub появился новый код — поставить
# его и перезапустить сайт. Если новая версия не поднялась за полторы минуты,
# вернуть прежнюю и больше не пытаться ставить этот коммит.
# Запускается таймером predicatorr-update.timer (см. install.sh).

set -euo pipefail

APP_DIR="${PREDICATOR_DIR:-/opt/predicatorr}"
APP_USER="${PREDICATOR_USER:-predicatorr}"
PM_ENV="${PREDICATOR_PARI_MIXER_ENV:-/etc/pari-mixer/env}"
PM_EXPORT="${PREDICATOR_PARI_MIXER_EXPORT:-http://127.0.0.1:8000/api/export/backup}"

as_app() { runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"; }
git_app() { as_app git -C "$APP_DIR" "$@"; }

env_value() { grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- || true; }

# Здоровье: сайт отвечает и модель обучилась (данные к этому моменту уже есть).
healthy() {
  local port password body
  port="$(env_value PREDICATOR_PORT)"
  password="$(env_value PREDICATOR_PASSWORD)"
  for _ in $(seq 1 90); do
    body="$(curl -s -f -u "admin:$password" "http://127.0.0.1:${port:-8100}/api/status" || true)"
    case "$body" in *'"ready":true'*) return 0 ;; esac
    sleep 1
  done
  return 1
}

# Свежая история прямо от pari-mixer (docs/pari-mixer-export.md): из его настроек берём
# только EXPORT_TOKEN. Возвращает 0, только если дописал источник в .env.
connect_pari_mixer() {
  local env_file="$APP_DIR/.env" token
  [ -f "$env_file" ] || return 1
  if grep -qE '^PREDICATOR_BACKUP_(URL|TOKEN)=' "$env_file"; then
    return 1
  fi
  [ -r "$PM_ENV" ] || return 1
  token="$({ grep -E '^EXPORT_TOKEN=' "$PM_ENV" || true; } | head -n 1 | cut -d= -f2- | tr -d "\r\"'")"
  [ -n "$token" ] || return 1
  # токен — заголовком из stdin, чтобы не светить его в списке процессов
  printf 'X-Export-Token: %s\n' "$token" | curl -fs -o /dev/null -m 20 -H @- "$PM_EXPORT" || return 1
  printf 'PREDICATOR_BACKUP_URL=%s\nPREDICATOR_BACKUP_TOKEN=%s\n' "$PM_EXPORT" "$token" >> "$env_file"
  echo "история матчей: подключена локальная выгрузка pari-mixer"
}

deploy() {
  local rev="$1"
  git_app reset --hard --quiet "$rev"
  as_app "$APP_DIR/.venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check \
    -r "$APP_DIR/requirements.txt"
  systemctl restart predicatorr
}

main() {
  local bad_file="$APP_DIR/var/bad-commit" old new
  # каждый запуск таймера, а не только при новом коммите: pari-mixer может появиться позже
  if connect_pari_mixer; then
    systemctl restart predicatorr
    healthy || echo "после подключения выгрузки сайт не ответил за полторы минуты" >&2
  fi
  git_app fetch --quiet origin
  git_app remote set-head origin --auto >/dev/null
  old="$(git_app rev-parse HEAD)"
  new="$(git_app rev-parse origin/HEAD)"
  [ "$old" != "$new" ] || exit 0
  if [ "$(cat "$bad_file" 2>/dev/null)" = "$new" ]; then
    echo "коммит ${new:0:7} уже не поднялся раньше — пропускаю"
    exit 0
  fi

  echo "обновление ${old:0:7} -> ${new:0:7}"
  deploy "$new"
  if healthy; then
    rm -f "$bad_file"
    echo "готово: $(git_app log -1 --format='%h %s')"
  else
    echo "новая версия не отвечает — возвращаю ${old:0:7}" >&2
    echo "$new" > "$bad_file"
    chown "$APP_USER": "$bad_file"
    deploy "$old"
    exit 1
  fi
}

main "$@"; exit $?
