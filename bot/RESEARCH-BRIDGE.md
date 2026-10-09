# Research Bridge — read-only мост Dusk ↔ Research Brain

`bot/research_bridge.py` — один stdlib-адаптер (плюс локальные модули
`frontmatter` и `learning_cycle`, без третьих зависимостей), который по вопросу
собирает **evidence records**: короткие фрагменты размеченной базы с проверяемым
происхождением.

Мост ничего не пишет: ни индекса, ни БД, ни копии, ни миграции. Никаких LLM,
сети, `subprocess`, git, API, Obsidian URI и создания задач. Содержимое заметок —
недоверенные данные: строки вида `ignore previous instructions`, HTML и команды
никогда не исполняются и в ответе остаются лишь ограниченной цитатой с
provenance.

## Что читается

| Источник | Путь | Слой |
|---|---|---|
| Research Brain | `raw/**` | `source` |
| Research Brain | `links/**` | `source` |
| Research Brain | `wiki/**/sources/**` | `source` |
| Research Brain | явный `type: source` / `source_collection` / `link` | `source` |
| Research Brain | `wiki/**/concepts/**`, `analysis/**`, `entities/**` или `type: concept/analysis/entity/synthesis` | `dusk_interpretation` |
| Research Brain | раздел/метаданные `author_summary` или `Рекомендация автора` | `author_summary` |
| Dusk | только подтверждённая пара stage-2B в `TaskNotes/Tasks/*.md` (pair identity + source + backlink) | `user_observation` |
| всё остальное | — | `unknown` (не угадывается) |

Слой по имени знаменитости **не** выводится: `author`/`domain` metadata дают
только метку домена (`sales/marketing` для Хормози, `ai/agents` для Карпати) и
никогда не повышают слой, score или истинность.

Слой `user_observation` ставится только подтверждённой паре stage-2B: пара,
источник и служебный backlink проверяются публичными функциями
`learning_cycle` (`application_rows`, `has_note_block`). Обычные TaskNotes,
ложные/external/traversal/symlink/повреждённые карточки в evidence и в index не
попадают вовсе и не разрешают relations. Факт `done` показывается отдельно от
наблюдения и вывода; отрицательный результат не скрывается и не
переименовывается в успех, причинность не заявляется.

Поля `observed`/`conclusion` нормализуются в одну безопасную строку
(`MAX_OBSERVATION`/`MAX_CONCLUSION`, 200 символов) до попадания в record и
Telegram: HTML/Markdown/wiki-control разметка снимается, усечение помечается `…`.

## Evidence record

```
{
  id, vault, path,            # стабильная identity, валт, POSIX-путь от корня
  title, heading,             # заголовок заметки и ближайший заголовок
  line_start, line_end,       # точные 1-based строки
  fragment, excerpt, score,   # ограниченная цитата и retrieval score
  layer,                       # source|author_summary|dusk_interpretation|user_observation|unknown
  relations: [{type,target,state,target_id,target_path,reason}],
  observations: [{done,status,completed,future,undated,observed,conclusion,
                  has_observation,has_conclusion,source}],
  domain, untrusted, conflicts
}
```

Сортировка детерминированная: `(-score, vault, path, line_start)`.

Identity: уникальный `dusk_id` из frontmatter → `dusk_id:<id>`, иначе
`<vault>:<относительный путь>`. Дубликат `dusk_id` считается ambiguous и для
разрешения relation не используется. ID в существующие файлы не пишется.

## Typed relations

Разбирается только явный блок `## Связи Dusk`. Обычный `Related`, keyword overlap
и прочие секции relations не создают.

```
## Связи Dusk

- supports: [[raw/источник]]
- contradicts: [[wiki/concepts/другое]]
- similar: id:01a1128b
- application: [[TaskNotes/Tasks/шаг]]
- result: [[links/итог]]
```

Русские алиасы: `подтверждает`/`поддерживает` → supports,
`противоречит`/`опровергает` → contradicts, `похоже`/`похожий` → similar,
`применение` → application, `результат` → result.

Блок `## Связи Dusk` уровня 2 заканчивается на следующем structural Markdown
heading равного или более высокого уровня (`#`/`##`) вне code fence; `###` его
не закрывает. Блок внутри ``` / ~~~ — пример или цитата, а не разметка связей.
Заголовок для provenance — последний настоящий structural heading вне code
fence (допускается до 3 ведущих пробелов); fenced-строка заголовком не считается.
Закрыть fence может только строка с тем же fence-символом и не короче
открывающего.

Markdown-структура (headings, fences, relations) начинается только после
корректно закрытого leading YAML frontmatter. YAML-скаляр — metadata/untrusted
data, а не relation-блок или provenance; `---` в середине тела frontmatter не
открывает. Незакрытый leading frontmatter — fail closed: headings/relations из
него не берутся, provenance остаётся пустым. Точные 1-based строки настоящего
тела при этом сохраняются; поля шапки читает отдельный `frontmatter`-парсер.

Разрешение по `id:<dusk_id>` переживает rename файла. Path-цель обязана быть
непустым vault-relative POSIX-путём без абсолютного префикса, Windows drive
(`C:`) и UNC, без NUL и без компонентов `.`/`..` (обратные слеши считаются
разделителями при проверке). Небезопасный target не разрешается и не идёт в
basename fallback: `state: missing` с причиной `небезопасный путь relation`, без
conflict output. Missing/ambiguous цель остаётся unresolved и показывается явно
(`state`, `reason`). Конфликт показывается только при явной `contradicts` в
любую из сторон — никакого семантического вывода о конфликте по словам,
позитивности или авторитету.

## Безопасность путей и чтения

Любой путь после `resolve()` обязан оставаться внутри разрешённого root.
Отсекаются: symlink наружу (в том числе symlink-каталоги), не-Markdown,
служебные каталоги (`.git`, `.obsidian`, `Private`, `SYSTEM`, `sessions`,
`Users`, `Flashcards`, `node_modules`, …), файлы больше `MAX_FILE_BYTES`
(200 000 байт), бинарные (NUL), нечитаемые и не-UTF-8. Причины пропуска
возвращает `diagnostics()` как безопасные строки, без содержимого.

Лимиты: `MAX_KEYWORDS = 12`, `MAX_RESULTS = 12`, `MAX_FRAGMENT = 240`,
`MAX_TITLE = 120`, `MAX_QUERY_CHARS = 500`, `MAX_HEADING = 120`,
`MAX_PATH = 240`, `MAX_DOMAIN = 60`, `MAX_OBSERVATION = 200`,
`MAX_CONCLUSION = 200`, `MAX_TARGET = 240`. Длина запроса ограничивается до
regex. Совпадение слов влияет только на retrieval score, но никогда на тип
relation, слой или истинность. Ответ Telegram (`ask.answer`) ограничен
`MAX_ANSWER_CHARS = 4000` (≤ 4096): усекается по целым результатам и явно
сообщает «ответ сокращён».

## Публичный API

```python
import research_bridge as rb

rb.search(query, limit=12, *, dusk=None, research=None, today=None)
rb.diagnostics(*, dusk=None, research=None)
rb.dusk_root(vault=None); rb.research_root(root=None); rb.detect_research_root(vault=None)
rb.parse_relations(text); rb.base_layer(rel, meta); rb.layer_for(rel, meta, heading, forced="")
rb.domain_for(meta); rb.query_terms(query); rb.safe_text(path, root); rb.walk_markdown(root, vault, allowed)
```

Корни: `VAULT_PATH` (Dusk) и `RESEARCH_BRAIN_PATH` (Research Brain); иначе
сосед `research-brain` рядом с валтом, иначе `~/Documents/obsidian/research-brain`.

## Команда `?вопрос`

`ask.py` сохраняет публичные `vaults()`, `search(query, limit=6)` и
`answer(query)`; поиск делегируется мосту. Старые ключи `vault, path, title,
excerpt, score` сохранены, добавлены `id, heading, line_start, line_end, layer,
relations, conflicts, observations, domain, untrusted`. Ответ короткий: ссылка
`[[точный/путь#заголовок]]`, строки, маркировка слоя, явный конфликт и результат
пользователя. Если доказательств нет, ответ честно об этом говорит. Задача из
совета автоматически не создаётся.

## Известные ограничения текущей базы

* В Research Brain пока нет `dusk_id` и блока `## Связи Dusk` — identity берётся
  из пути, typed relations и конфликты появятся только после явной разметки.
* Нет файлов с `author_summary` / `Рекомендация автора` — слой «рекомендация
  автора» не встречается, пока он не размечен.
* В `TaskNotes/Tasks` пока нет подтверждённых карточек `duskApply: 2b` — слой
  `user_observation` появится, когда пары начнут создаваться через stage-2B.
* Каждый запрос перечитывает размеченные файлы заново (индекс запрещён
  контрактом); объём ограничен размером файла и числом результатов, а не общим
  бюджетом байт.
* При большом числе результатов Telegram-ответ усекается по целым блокам до
  `MAX_ANSWER_CHARS` и явно сообщает о сокращении.
