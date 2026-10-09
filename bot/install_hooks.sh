#!/bin/bash
# Ставит git-хук pre-commit с проверкой приватности.
#
# Нужен отдельный установщик, потому что папка .git/hooks не версионируется:
# через git она не приедет ни на другой Mac, ни на VPS. Скрипт лежит в репозитории,
# а хук создаётся локально.

set -e

VAULT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$VAULT/.git/hooks/pre-commit"

if [ ! -d "$VAULT/.git/hooks" ]; then
  echo "Не git-репозиторий: $VAULT" >&2
  exit 1
fi

PY=/usr/bin/python3
[ -x "$PY" ] || PY="$(command -v python3)"

cat > "$HOOK" <<EOF
#!/bin/bash
# Создан bot/install_hooks.sh — не редактировать вручную.
exec $PY "$VAULT/bot/check_privacy.py"
EOF

chmod +x "$HOOK"
echo "pre-commit установлен: $HOOK"
echo "интерпретатор: $PY ($($PY -V 2>&1))"