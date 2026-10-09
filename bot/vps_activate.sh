#!/bin/bash
# Активация Telegram-бота на VPS.
#
# Зачем отдельный скрипт: сервис уже создан и проверен, не хватает только токена.
# Скрипт делает последний шаг идемпотентно — можно запускать повторно.
#
# Использование (на VPS):
#     bash bot/vps_activate.sh 123456789:AA...токен...
#
# Токен берётся у @BotFather в Telegram: /newbot.
# ВАЖНО: нужен НОВЫЙ бот. Переиспользовать токен другого сервиса нельзя —
# Telegram отдаёт updates только одному получателю, и боты начнут воровать
# сообщения друг у друга.

set -e

VAULT="${VAULT_PATH:-/root/dusk-vault}"
SERVICE="dusk-vault-bot"
TOKEN="$1"

if [ -z "$TOKEN" ]; then
  echo "Не передан токен." >&2
  echo "Использование: bash bot/vps_activate.sh <TELEGRAM_BOT_TOKEN>" >&2
  exit 1
fi

# грубая проверка формата: <цифры>:<строка>
if ! printf '%s' "$TOKEN" | grep -qE '^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$'; then
  echo "Токен не похож на Telegram bot token (ожидается 123456789:AA...)." >&2
  exit 1
fi

cd "$VAULT"

if [ ! -f .env ]; then
  echo "Нет $VAULT/.env — сначала создай его (TELEGRAM_USER_ID)." >&2
  exit 1
fi

# 1. подставить токен, не трогая остальные переменные
python3 - "$TOKEN" <<'PY'
import pathlib, sys
token = sys.argv[1]
p = pathlib.Path("/root/dusk-vault/.env")
lines = [l for l in p.read_text().splitlines() if not l.startswith("TELEGRAM_BOT_TOKEN=")]
lines.append("TELEGRAM_BOT_TOKEN=" + token)
p.write_text("\n".join(lines) + "\n")
p.chmod(0o600)
print("  токен записан в .env")
PY

# 2. не дать токену уехать в git
if ! grep -q '^\.env$' .gitignore 2>/dev/null; then
  echo ".env" >> .gitignore
fi
if [ -n "$(git status --porcelain .env 2>/dev/null)" ]; then
  echo "  ВНИМАНИЕ: .env виден git — проверь .gitignore" >&2
else
  echo "  .env вне git — ок"
fi

# 3. запустить
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true

# 4. часовой пояс: VPS обычно живёт в Europe/London, а валт ведётся в Asia/Almaty.
# Без этого время записи в инбоксе врёт на 4 часа, а инбокс хронологический —
# по времени ты понимаешь, когда именно пришла мысль. Делаем drop-in,
# чтобы не переписывать сам юнит.
TZ_WANTED="${VAULT_TZ:-Asia/Almaty}"
mkdir -p "/etc/systemd/system/$SERVICE.service.d"
cat > "/etc/systemd/system/$SERVICE.service.d/timezone.conf" <<EOF
[Service]
Environment=TZ=$TZ_WANTED
EOF
systemctl daemon-reload
echo "  часовой пояс сервиса: $TZ_WANTED"

systemctl restart "$SERVICE"
sleep 6

STATE="$(systemctl is-active "$SERVICE")"
echo "  сервис: $STATE / $(systemctl is-enabled "$SERVICE" 2>&1)"

if [ "$STATE" = "active" ]; then
  echo "  ✔ бот работает. Напиши ему в Telegram — сообщение должно упасть в Inbox."
else
  echo "  ✗ не поднялся. Последние строки лога:"
  tail -15 /root/dusk-vault-bot.log 2>/dev/null | sed 's/^/    /'
  exit 1
fi
