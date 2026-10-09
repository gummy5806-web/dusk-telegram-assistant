#!/bin/bash
# Разбор очереди обучающих видео — на Mac (yt-dlp + свой Vertex).
set -e
VAULT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
[ -x "$PY" ] || { echo "нет интерпретатора с Vertex SDK" >&2; exit 1; }
cd "$VAULT/bot"
VAULT_PATH="${VAULT_PATH:-$VAULT}" LLM_PROVIDER=vertex exec "$PY" -c "
import sys; sys.path.insert(0,'.')
import learn
out = learn.process_queue()
print(out if out else 'Очередь пуста')
"
