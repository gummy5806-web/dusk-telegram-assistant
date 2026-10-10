# Проверка локального репозитория — 9 октября 2026

- Полный набор Python-тестов на PTB 21.5: **1089 tests, OK, без skip**.
- Swift DuskCore: **23/23 PASS**; debug-сборка DuskPanel: **PASS**.
- Python compileall для бота, прокси и legacy-панели: **PASS**.
- Shell `bash -n`, чтение plist через plistlib: **PASS**.
- SHA-256 исходных 187 файлов: **0 расхождений**.
- Скан токенов распространённых провайдеров, private-key blocks и присваиваний
  ключей: **реальных совпадений нет**. Два совпадения — явно синтетические
  `SECRET`-маркеры в `test_private_metadata.py` и `test_stage5_interface.py`.
- Игнорирование `.env`, credentials, keys, runtime, Private и сборок проверено.
- Существующий whitespace сторонних плагинов, шаблонов и архивных документов
  сохранён побайтно; только для этих snapshots отключена whitespace-проверка
  через `.gitattributes`. Для кода бота, Swift и новых документов она включена.

Во время сохранения программа и proxy не запускались против рабочих данных,
приложения не устанавливались, сервисы не перезапускались. Локальные тесты
использовали временные валты/mocks; с VPS read-only прочитан только proxy source
и номер версии его зависимости. GitHub remote и push не создавались.

## Обновление chat-controls — 9 октября 2026

- До реализации 12 новых PTB-регрессий: 8 failures, 3 errors (RED).
- Финальный полный набор на PTB 21.5: **1132 tests, OK, без skip**.
- 21 исполняемый Telegram-тест: inline меню, удаление нижней клавиатуры,
  новое/существующее напоминание, звуковой delivery, точная запись после голоса,
  correction/delete/restore, отмена/replay, недоступность для чужого пользователя.
- 22 thought tests: точная запись, сохранение frontmatter/context/links,
  stale/duplicate/no-confirmation, восстановление с новыми соседями и после
  рестарта, повреждённые backup shapes, symlink/Private boundary и >200 заметок.
- `compileall`, `git diff --check`: PASS. Скан секретов: 0 token findings.
- Source manifest обновлён для изменённых файлов; исходные hashes сохранены
  в `initial_source_sha256` для воспроизводимого происхождения снимка.

Независимое ревью и выкладка фиксируются в отчёте приёмки, отдельно от этих
локальных проверок. Реальные пользовательские мысли/задачи тестами не создавались.

## Приёмка и выкладка chat-controls

Независимый reviewer: **ACCEPT**, 43/43 focused tests. На VPS полный набор
прошёл в изоляции и после установки: **1132/1132** в обоих запусках.
87 отслеживаемых Python-модулей скомпилированы; 16/16 установленных SHA-256
совпали. При интеграции 842 файла вне патча совпали побайтно по SHA-256.
Сервис active/running, NRestarts=0; новое inline menu реально доставлено
в чат владельца (message 76). См. `docs/history/2026-10-09-chat-controls-acceptance.md`.

## Разбор мыслей — 10 октября 2026

1175/1175 Python tests на PTB 21.5, без skip. Из них 43 новых tests core/queue/transport/delivery. RED воспроизведён для отсутствующего интерфейса, cancellation и backlog; meaningful source-identity, created-time, malformed queue и concurrency regressions прошли. Read-only reviewer: ACCEPT после исправлений. Python compile/diff-check и LaunchAgent plist lint — PASS. Реальная модель, установка и доставка фиксируются в отдельной приёмке.


## Финальная проверка thought-digest

10 октября 2026: **1181/1181** tests PASS на Mac (PTB 21.5) и после установки
на VPS (Python 3.10 / PTB 21.5). 95 tracked VPS Python modules compile PASS,
17/17 installed hashes совпали на Mac и VPS; 855 tracked файлов вне итогового
патча не изменились. `compileall`, `git diff --check`, plist lint PASS.

Реальный UAT обнаружил нестабильность свободного JSON, поэтому только digest
передаёт существующему Vertex SDK `response_mime_type=application/json` и
`response_schema`; прочие вызовы модели сохраняют прежний интерфейс. Проверка
ссылок/размеров не ослаблена. Дополнительная регрессия исключает guide/template.
Последний реальный запрос дал корректные намерения, вопросы и отложенное;
неясная расшифровка предложена к уточнению, старый deadline не стал новым.
Основная реализация получила отдельный ACCEPT; последние API/prompt/guide
поправки проверены Codex полным набором и реальными вызовами, без заявления
о повторном независимом ревью. Итог доставки — в отдельной приёмке.


## Исправление происхождения записей

1186/1186 Mac/VPS PASS. 5 новых регрессий: source-only input, button exclusion, legacy button routing, multiline/code fence, empty capture. Реальные коллекции не содержат спорных записей/задач и агентских sections. Архив 4/4 SHA-256, установленный код 4/4 SHA-256; service active/NRestarts=0. См. docs/history/2026-10-10-capture-origin-fix.md.
