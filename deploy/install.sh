#!/usr/bin/env bash
# Установка и обновление Predicatorr на сервере с Ubuntu/Debian. Запуск от root:
#
#   curl -fsSL https://raw.githubusercontent.com/nolovedeepweb111/predicatorr/HEAD/deploy/install.sh \
#     | sudo bash -s -- mixer-predicts.duckdns.org
#
# Что делает: ставит пакеты, заводит пользователя predicatorr, кладёт код в
# /opt/predicatorr, поднимает службу systemd, nginx с HTTPS (Let's Encrypt) для
# домена и автообновление из GitHub раз в 5 минут. Повторный запуск безопасен
# (можно без аргументов): обновляет код и перезапускает сайт, база, пароль и
# домен остаются.
#
# Скрипт рассчитан на сервер, где уже живут другие сайты: свободный порт он
# ищет сам (начиная с 8100), чужие конфиги nginx не трогает, nginx не
# перезапускает, а перечитывает, и только после успешного nginx -t.
#
# Без домена сайт откроется напрямую на http://IP:порт.
# Переменные: PREDICATOR_AUTO_UPDATE=0 — без автообновления;
# PREDICATOR_TAKEOVER=1 — если домен уже занят другим сайтом nginx, отключить тот сайт.

set -euo pipefail

REPO="${PREDICATOR_REPO:-https://github.com/nolovedeepweb111/predicatorr.git}"
APP_DIR="${PREDICATOR_DIR:-/opt/predicatorr}"
APP_USER="${PREDICATOR_USER:-predicatorr}"
PORT="${PREDICATOR_PORT:-8100}"
AUTO_UPDATE="${PREDICATOR_AUTO_UPDATE:-1}"
TAKEOVER="${PREDICATOR_TAKEOVER:-0}"
ACME_ROOT=/var/www/letsencrypt

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"; }

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }

# 0 — порт слушает кто-то, кроме нашего сайта (свой процесс узнаём по пользователю).
port_taken() {
  local line pid
  line="$(ss -ltnpH "sport = :$1" 2>/dev/null | head -1)"
  [ -n "$line" ] || return 1
  pid="$(sed -nE 's/.*pid=([0-9]+).*/\1/p' <<<"$line" | head -1)"
  if [ -n "$pid" ] && [ "$(ps -o user= -p "$pid" | tr -d ' ')" = "$APP_USER" ]; then
    return 1
  fi
  return 0
}

pick_port() {
  local p="$1"
  while port_taken "$p"; do p=$((p + 1)); done
  echo "$p"
}

# Порты 80 и 443 должен держать nginx (или никто): иначе ставить nginx нельзя.
check_web_ports() {
  [ -n "$DOMAIN" ] || return 0
  local p line
  for p in 80 443; do
    line="$(ss -ltnpH "sport = :$p" 2>/dev/null | head -1)"
    [ -n "$line" ] || continue
    if ! grep -q '"nginx"' <<<"$line"; then
      die "порт $p занят не nginx ($(sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' <<<"$line")). Пришлите это сообщение — подстроим установку под ваш веб-сервер."
    fi
  done
}

# Другие сайты nginx, которые уже отвечают на наш домен.
domain_conflicts() {
  local pattern f
  pattern="server_name[^;]*[[:space:]]${DOMAIN//./\\.}([[:space:];]|$)"
  { grep -RlsE "$pattern" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null || true; } |
    while read -r f; do
      [ "$(readlink -f "$f")" = /etc/nginx/sites-available/predicatorr ] || echo "$f"
    done
}

nginx_apply() {
  nginx -t -q || die "nginx не принял конфигурацию, ничего не перечитываю (sudo nginx -t покажет причину)"
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx
  else
    systemctl enable --quiet nginx
    systemctl start nginx
  fi
}

install_packages() {
  log "Пакеты"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv curl ca-certificates openssl >/dev/null
  if [ -n "$DOMAIN" ]; then
    apt-get install -y -qq nginx certbot >/dev/null
  fi
  PY=python3
  if ! python_ok "$PY"; then
    if grep -qi '^ID=ubuntu' /etc/os-release; then
      log "Системный Python старше 3.10 — ставлю python3.11 из deadsnakes"
      apt-get install -y -qq software-properties-common >/dev/null
      add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
      apt-get update -qq
      apt-get install -y -qq python3.11 python3.11-venv >/dev/null
      PY=python3.11
    else
      die "нужен Python 3.10 или новее, в системе $(python3 -V 2>&1). Подойдут Ubuntu 22.04+ или Debian 12."
    fi
  fi
}

install_code() {
  log "Код в $APP_DIR"
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi
  mkdir -p "$APP_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    chown -R "$APP_USER": "$APP_DIR"
    as_app git -C "$APP_DIR" fetch --quiet origin
    as_app git -C "$APP_DIR" remote set-head origin --auto >/dev/null
    as_app git -C "$APP_DIR" reset --hard --quiet origin/HEAD
  else
    [ -z "$(ls -A "$APP_DIR")" ] || die "$APP_DIR не пустая и это не наш репозиторий — освободите папку"
    chown "$APP_USER": "$APP_DIR"
    as_app git clone --quiet "$REPO" "$APP_DIR"
  fi
  echo "версия: $(as_app git -C "$APP_DIR" log -1 --format='%h %s')"

  if [ -x "$APP_DIR/.venv/bin/python" ] && ! python_ok "$APP_DIR/.venv/bin/python"; then
    rm -rf "$APP_DIR/.venv"
  fi
  [ -x "$APP_DIR/.venv/bin/python" ] || as_app "$PY" -m venv "$APP_DIR/.venv"
  as_app "$APP_DIR/.venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check \
    -r "$APP_DIR/requirements.txt"
  mkdir -p "$APP_DIR/var"
  chown -R "$APP_USER": "$APP_DIR"
}

write_env() {
  local env_file="$APP_DIR/.env" host=127.0.0.1
  [ -n "$DOMAIN" ] || host=0.0.0.0
  if [ ! -f "$env_file" ]; then
    log "Настройки и пароль"
    local password
    PORT="$(pick_port "$PORT")"
    password="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 16)"
    cat > "$env_file" <<EOF
PREDICATOR_HOST=$host
PREDICATOR_PORT=$PORT
PREDICATOR_PASSWORD=$password
PREDICATOR_SYNC_MINUTES=15
PREDICATOR_MIXER_LIVE=1
PARI_ENABLED=1
EOF
  fi
  # домен запоминаем, чтобы повторный запуск без аргументов ничего не сломал
  if grep -q '^PREDICATOR_DOMAIN=' "$env_file"; then
    sed -i "s|^PREDICATOR_DOMAIN=.*|PREDICATOR_DOMAIN=$DOMAIN|" "$env_file"
  else
    echo "PREDICATOR_DOMAIN=$DOMAIN" >> "$env_file"
  fi
  chown "$APP_USER": "$env_file"
  chmod 600 "$env_file"
  PASSWORD="$(grep -E '^PREDICATOR_PASSWORD=' "$env_file" | cut -d= -f2- || true)"
  PORT="$(grep -E '^PREDICATOR_PORT=' "$env_file" | cut -d= -f2- || true)"
  PORT="${PORT:-8100}"
  if port_taken "$PORT"; then
    local moved
    moved="$(pick_port 8100)"
    warn "порт $PORT занят другой программой — сайт переезжает на $moved"
    sed -i "s|^PREDICATOR_PORT=.*|PREDICATOR_PORT=$moved|" "$env_file"
    PORT="$moved"
  fi
  echo "порт сайта: $PORT"
}

install_service() {
  log "Служба predicatorr"
  cat > /etc/systemd/system/predicatorr.service <<EOF
[Unit]
Description=Predicatorr: прогнозы на Dota-миксеры
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m predicator serve
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/var

[Install]
WantedBy=multi-user.target
EOF

  if [ "$AUTO_UPDATE" = "1" ]; then
    cat > /etc/systemd/system/predicatorr-update.service <<EOF
[Unit]
Description=Predicatorr: обновление из GitHub

[Service]
Type=oneshot
Environment=PREDICATOR_DIR=$APP_DIR PREDICATOR_USER=$APP_USER
ExecStart=/bin/bash $APP_DIR/deploy/update.sh
EOF
    cat > /etc/systemd/system/predicatorr-update.timer <<'EOF'
[Unit]
Description=Predicatorr: проверять обновления каждые 5 минут

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  fi
  systemctl daemon-reload
  systemctl enable --quiet predicatorr
  systemctl restart predicatorr
  if [ "$AUTO_UPDATE" = "1" ]; then
    systemctl enable --quiet --now predicatorr-update.timer
  else
    systemctl disable --quiet --now predicatorr-update.timer 2>/dev/null || true
  fi
}

# IPv6 слушаем, только если он есть в системе: иначе nginx не стартует.
listen_lines() {
  echo "    listen $1;"
  [ -e /proc/net/if_inet6 ] && echo "    listen [::]:$1;"
  return 0
}

nginx_http_only() {
  cat > /etc/nginx/sites-available/predicatorr <<EOF
server {
$(listen_lines 80)
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        include /etc/nginx/predicatorr-proxy.conf;
    }
}
EOF
}

nginx_https() {
  cat > /etc/nginx/sites-available/predicatorr <<EOF
server {
$(listen_lines 80)
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
$(listen_lines "443 ssl")
    server_name $DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        include /etc/nginx/predicatorr-proxy.conf;
    }
}
EOF
}

install_nginx() {
  [ -n "$DOMAIN" ] || return 0
  log "nginx и HTTPS для $DOMAIN"
  mkdir -p "$ACME_ROOT"
  cat > /etc/nginx/predicatorr-proxy.conf <<'EOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_read_timeout 180s;
EOF
  local conflicts f
  conflicts="$(domain_conflicts)"
  if [ -n "$conflicts" ]; then
    if [ "$TAKEOVER" != "1" ]; then
      die "домен $DOMAIN уже обслуживает другой сайт nginx: $(echo "$conflicts" | tr '\n' ' ')
Если его нужно заменить нашим, запустите установку с PREDICATOR_TAKEOVER=1."
    fi
    mkdir -p /etc/nginx/disabled-by-predicatorr
    while read -r f; do
      if [ -L "$f" ]; then
        rm "$f"
        warn "отключён сайт nginx $f (файл остался в sites-available, вернуть: ln -s)"
      else
        mv "$f" /etc/nginx/disabled-by-predicatorr/
        warn "отключён сайт nginx $f → /etc/nginx/disabled-by-predicatorr/"
      fi
    done <<<"$conflicts"
  fi
  if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then nginx_https; else nginx_http_only; fi
  ln -sf /etc/nginx/sites-available/predicatorr /etc/nginx/sites-enabled/predicatorr
  # Без IPv6 стандартный сайт nginx (listen [::]:80) не даёт ему запуститься.
  if [ ! -e /proc/net/if_inet6 ] && [ -f /etc/nginx/sites-available/default ]; then
    sed -i -E 's/^([[:space:]]*listen[[:space:]]+\[::\]:)/# \1/' /etc/nginx/sites-available/default
  fi
  nginx_apply

  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
  fi

  if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    if certbot certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos \
        --register-unsafely-without-email --keep-until-expiring \
        --deploy-hook "systemctl reload nginx" >/tmp/predicatorr-certbot.log 2>&1; then
      nginx_https
      nginx_apply
      HTTPS=1
    else
      warn "Сертификат не выпущен (лог: /tmp/predicatorr-certbot.log). Сайт пока работает по http://$DOMAIN."
      warn "Проверьте, что $DOMAIN указывает на этот сервер и порт 80 открыт, и запустите скрипт ещё раз."
    fi
  else
    HTTPS=1
  fi
}

wait_healthy() {
  local code
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$PASSWORD" "http://127.0.0.1:$PORT/api/status" || true)"
    [ "$code" = "200" ] && return 0
    sleep 1
  done
  return 1
}

main() {
  DOMAIN="${1:-${PREDICATOR_DOMAIN:-}}"
  if [ -z "$DOMAIN" ] && [ -f "$APP_DIR/.env" ]; then
    DOMAIN="$(grep -E '^PREDICATOR_DOMAIN=' "$APP_DIR/.env" | cut -d= -f2- || true)"
  fi
  HTTPS=0
  PY=python3
  [ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash install.sh <домен>"
  command -v apt-get >/dev/null || die "скрипт рассчитан на Ubuntu/Debian"
  command -v systemctl >/dev/null || die "нужен systemd"

  check_web_ports
  install_packages
  install_code
  write_env
  install_service
  install_nginx

  log "Проверка"
  if wait_healthy; then
    echo "сайт отвечает; первая загрузка истории матчей занимает около минуты"
  else
    warn "сайт не ответил за минуту — смотрите журнал: journalctl -u predicatorr -n 50"
  fi

  local url
  if [ -z "$DOMAIN" ]; then
    url="http://$(hostname -I | awk '{print $1}'):$PORT"
  elif [ "$HTTPS" = "1" ]; then
    url="https://$DOMAIN"
  else
    url="http://$DOMAIN"
  fi
  local mem_mb
  mem_mb="$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)"

  printf '\n\033[1;32mГотово.\033[0m\n'
  printf '  Сайт:    %s\n' "$url"
  printf '  Логин:   любой\n'
  printf '  Пароль:  %s   (хранится в %s/.env)\n' "$PASSWORD" "$APP_DIR"
  printf '  Журнал:  journalctl -u predicatorr -f\n'
  if [ "$AUTO_UPDATE" = "1" ]; then
    printf '  Обновления из GitHub ставятся сами раз в 5 минут (journalctl -u predicatorr-update).\n'
  else
    printf '  Обновить: запустите этот скрипт ещё раз.\n'
  fi
  if [ "$mem_mb" -lt 1500 ]; then
    warn "На сервере ${mem_mb} МБ памяти. Если загрузка линии PARI будет падать, выключите её: PARI_ENABLED=0 в $APP_DIR/.env"
  fi
}

# Вся логика в main: bash дочитывает файл до запуска, поэтому скрипт можно
# обновлять во время работы и запускать через curl | bash.
main "$@"; exit $?
