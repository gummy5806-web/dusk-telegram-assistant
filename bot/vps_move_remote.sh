#!/bin/bash
# Одноразовый переезд VPS на новый репозиторий.
#
# ЗАЧЕМ: локальная история была переписана (из неё вычищены секреты),
# поэтому клон на VPS разошёлся с новым origin и обычный pull не сработает.
#
# Запускать НА VPS, один раз:
#     bash bot/vps_move_remote.sh            # если валт в /root/dusk-vault
#     bash bot/vps_move_remote.sh /путь/к/валту
#
# Что НЕ трогается: .env (секреты) и bot/venv (зависимости) — они не в git,
# и reset --hard их не удаляет.

set -e

NEW_REMOTE="${NEW_REMOTE:-https://github.com/gummy5806-web/dusk-vault.git}"
VAULT="${1:-/root/dusk-vault}"

if [ ! -d "$VAULT/.git" ]; then
  echo "Это не git-репозиторий: $VAULT" >&2
  exit 1
fi

cd "$VAULT"

echo "валт      : $VAULT"
echo "старый    : $(git remote get-url origin 2>/dev/null || echo '(нет)')"
echo "новый     : $NEW_REMOTE"

# Сохраняем локальные коммиты VPS (их создаёт бот при захвате), чтобы ничего не потерять
LOCAL_COMMITS="$(git log origin/master..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')"
if [ "$LOCAL_COMMITS" != "0" ]; then
  BUNDLE="/root/vps-local-$(date +%F-%H%M).bundle"
  git bundle create "$BUNDLE" --all >/dev/null 2>&1 || true
  echo "локальных коммитов на VPS: $LOCAL_COMMITS — сохранены в $BUNDLE"
else
  echo "локальных коммитов на VPS нет"
fi

git remote set-url origin "$NEW_REMOTE"
git fetch origin

# Перед сбросом: страховочная копия того, что сейчас в рабочем дереве
if [ -n "$(git status --porcelain)" ]; then
  STASH="/root/vps-worktree-$(date +%F-%H%M).patch"
  git diff > "$STASH" 2>/dev/null || true
  echo "незакоммиченные правки сохранены в $STASH"
  git checkout -- . 2>/dev/null || true
fi

git reset --hard origin/master

echo
echo "готово. теперь HEAD = $(git rev-parse --short HEAD)"
echo "проверка: git status -sb"
git status -sb | head -3
echo
echo "секреты на месте (не в git, reset их не тронул):"
[ -f .env ] && echo "  ✔ .env есть ($(grep -c '=' .env) переменных)" || echo "  ⚠ .env НЕТ — создай с TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID"
[ -d bot/venv ] && echo "  ✔ bot/venv есть" || echo "  ⚠ bot/venv НЕТ — python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt"
