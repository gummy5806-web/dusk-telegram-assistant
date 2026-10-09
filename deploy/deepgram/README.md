# Deepgram proxy snapshot

Исходник скопирован read-only с `/opt/dsh/deepgram_proxy.py` на VPS 9 октября 2026.
Файл ключей и конфигурация с секретами не переносились. REST и WebSocket transport,
round-robin, quarantine/revival — в самом исходнике.

Переменные: `DEEPGRAM_KEYS_FILE`, `DEEPGRAM_PROXY_PORT`, `DEEPGRAM_PROXY_CLIENT_KEY`,
`DEEPGRAM_QUARANTINE_SECONDS`, `DEEPGRAM_UPSTREAM`, `DEEPGRAM_WS_UPSTREAM`.
Ключи загружаются из внешнего текстового файла; он исключён из Git.

Это архив существующей инфраструктурной логики. В ходе сохранения проверялись
синтаксис и версия зависимости; сервис не перезапускался, внешние запросы не выполнялись.

Зависимость действующего VPS: `websockets==16.1.1`, зафиксирована в `requirements.txt`.
