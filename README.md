# Dusk Assistant

Локальный репозиторий исходников Dusk: Telegram-ассистент, личная память в Obsidian,
канбан, фокус, напоминания, проверяемое обучение и Mac-панель Caps+N.
Снимок принятого кода от 9 октября 2026. GitHub-публикация ещё не выполнена.

## Что сохранено

- `bot/`: вся Python-логика, тесты, зависимости и служебные shell-скрипты.
- `config/dusk-panel/`: исходники Swift, тесты ядра и Info.plist панели.
- `scripts/`: сборка/установка панели, настройка BetterTouchTool и восстановление валта.
- `desktop/launchagents/`: шесть существующих Mac LaunchAgents; `desktop/legacy/`: старый Tk-захват.
- `Board/`, `Пульт.md`, `SYSTEM/TEMPLATE/`, `.obsidian/snippets/`: доска, инструкции и шаблоны.
- `.obsidian/plugins/`: снимки TaskNotes 4.5.1 и Kanban Bases View 0.10.6.
  Код сторонних плагинов сохранён вместе с manifest; настройка TaskNotes очищена
  по разрешённому списку полей, API/MCP отключены, токены и OAuth не перенесены.
- `deploy/deepgram/`: исходник работающего прокси без файла ключей.
- `docs/specs/`, `docs/plans/`, `docs/history/`: дизайн, планы и приёмка.
- `docs/SOURCE-MANIFEST.json`: исходные версии и SHA-256 скопированных файлов.

Личные мысли, задачи, конспекты Research Brain, `.env`, ADC, ключи Deepgram,
SQLite-состояние, логи, BTT database и прежние Git-истории сюда не входят.
Этот репозиторий сохраняет программу; пользовательские данные остаются в валте.
Документы в `docs/history` описывают состояние на момент своей записи.

## Как устроен цикл

Свободный текст/голос → Inbox; кнопка «Мысль» → отдельная fleeting note.
«Мысли» возвращает Inbox, последние и забытые записи, даёт поиск и перевод в задачу.
Карточка задачи → фокус или напоминание; «Статус» → текущая личная сводка.
Research Brain подключается к явному вопросу о знаниях. Недельный обзор связывает
источник, применение, действие и записанный пользователем результат.

Transport: `telegram_bot.py`, Swift-панель, `dispatch.py`, `speech_to_text.py`.
Business logic: `thought_feed.py`, `personal_search.py`, `personal_summary.py`,
`focus.py`, `reminders.py`, `learning_cycle.py`, `weekly_plan.py`, `weekly_review.py`.
Markdown TaskNotes остаётся источником истины; `runtime_store.py` хранит SQLite-проекции,
cooldown, поколения и очередь доставки. Scheduler проверяет актуальность перед отправкой.
LLM вызывается в фоновых шагах через `llm.py`; личная сводка и базовый захват детерминированы.

## Проверка локальной копии

Python 3.10+ для VPS; зависимости зафиксированы в `bot/requirements.txt`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r bot/requirements.txt
.venv/bin/python -m unittest discover -s bot -p 'test*.py'
.venv/bin/python -m compileall -q bot deploy/deepgram
swift run --package-path config/dusk-panel DuskCoreTests
swift build --package-path config/dusk-panel --product DuskPanel
git diff --check
```

Тесты работают на временных валтах и mock-транспорте. Запуск рабочего бота,
install-скриптов и LaunchAgents в эти команды не входит.

## Восстановление

1. Отдельно восстановить валт данных и Research Brain из их резервных копий.
2. Скопировать `bot/` и выбранные шаблоны/доску в валт; сохранить новые пользовательские настройки.
3. Создать `.env` по `.env.example`, вне Git восстановить credentials и runtime DB.
   Не запускать бот на исходниковом репозитории: его transport синхронизирует рабочий валт.
4. На Mac собрать панель через `scripts/build-dusk-panel.sh` либо установить из исходников
   через `DUSK_BUILD_FROM_SOURCE=1 bash scripts/install-dusk-panel.sh`.
   Настроить Caps+N/P/A через `scripts/setup-btt-dusk.sh` при установленном BetterTouchTool/jq.
5. LaunchAgents содержат пути текущего Mac. Перед установкой адаптировать их для нужного пользователя.
   Shell-скрипты deploy/activation имеют реальные побочные эффекты; их не запускают для тестов.
6. Прокси требует отдельный файл `DEEPGRAM_KEYS_FILE`; бот использует только локальный URL и client key.

## Git

Новая история начинается с очищенного локального снимка. `.gitignore` исключает
секреты, личные записи, базы и сборки. Даже приватный GitHub получает только исходники.
Перед публикацией повторить скан секретов и проверить список `git ls-files`.
Для полного локального переноса истории: `git bundle create ../dusk-assistant.bundle --all`.
