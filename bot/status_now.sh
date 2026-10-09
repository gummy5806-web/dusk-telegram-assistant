#!/bin/bash
# Пересобрать страницу статуса на Mac через свой Vertex.
# Числа считаются локально, модель нужна только для суждения (цели, три дела, что выбросить).
set -e
VAULT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
[ -x "$PY" ] || { echo "нет интерпретатора с Vertex SDK: $PY" >&2; exit 1; }
cd "$VAULT/bot"
VAULT_PATH="${VAULT_PATH:-$VAULT}" LLM_PROVIDER=vertex exec "$PY" -c "
import sys; sys.path.insert(0,'.')
import status
status.build_status()
print('Статус обновлён')
"
