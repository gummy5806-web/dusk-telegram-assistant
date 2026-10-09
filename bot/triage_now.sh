#!/bin/bash
# Разбор инбокса НА MAC через свой Vertex.
#
# Почему на Mac: личный контент не должен уходить сторонним API.
# Vertex авторизуется через твой gcloud (ADC) и работает в твоём GCP-проекте.
# На VPS этого нет — и это осознанно: там разбор не запускается.
#
# Запуск:  bash bot/triage_now.sh
# Автоматически: com.dusk.triage (launchd), если установлен.

set -e

VAULT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3

if [ ! -x "$PY" ]; then
  echo "Не нашёл интерпретатор с Vertex SDK: $PY" >&2
  exit 1
fi

cd "$VAULT/bot"
VAULT_PATH="${VAULT_PATH:-$VAULT}" LLM_PROVIDER=vertex exec "$PY" - <<'PY'
import sys
sys.path.insert(0, ".")
import triage
print(triage.run_triage())
PY
