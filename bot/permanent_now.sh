#!/bin/bash
# Сборка черновиков permanent-заметок из повторяющихся выводов — на Mac (свой Vertex).
set -e
VAULT="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
[ -x "$PY" ] || { echo "нет интерпретатора с Vertex SDK" >&2; exit 1; }
cd "$VAULT/bot"
VAULT_PATH="${VAULT_PATH:-$VAULT}" LLM_PROVIDER=vertex exec "$PY" -c "
import sys; sys.path.insert(0,'.')
import permanent
print(permanent.draft_permanents())
"
