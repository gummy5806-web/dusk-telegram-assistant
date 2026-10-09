# Dusk Personal Assistant — design

## Problem

The Telegram bot currently behaves like a capture form. It stores plain text and
voice in Inbox, but natural recall is routed to Research Brain. This reverses the
owner's mental model: Dusk is the personal system (thoughts, ideas, tasks and
plans); Research Brain is an internal store of agent-created study notes and
relations.

The user must never need punctuation commands such as `задача:` or `найди:`.
Legacy forms may remain as hidden compatibility paths, but `/start`, keyboards,
prompts and documentation must not teach them.

## Considered approaches

### 1. Dusk-first deterministic assistant — selected

Natural-language intent detection handles a small set of reliable actions:
capture, personal recall, explicit task creation and study-knowledge questions.
All other input is safely captured in Dusk. Search is local, bounded and
read-only. Research Brain is consulted only for study/author/term questions and
never replaces personal results.

This gives predictable writes and natural interaction without allowing an LLM
to invent a destructive intent.

### 2. LLM routes every message

This is more conversational but can misclassify a thought as a task or a search
as a write. It also makes basic capture depend on an external service. Rejected.

### 3. Buttons and command grammar

This is easy to test but forces the owner to remember syntax and does not behave
like an assistant. It is the current failure mode. Rejected.

## User contract

### Default scope

- Dusk is always the primary personal memory.
- Research Brain is an internal secondary knowledge source containing
  agent-created summaries, topics, terms and source relations.
- `Private/`, configuration, caches and system files are never searched.

### Natural input

- A normal statement or voice message is captured in the Dusk Inbox.
- `создай задачу купить кабель` creates a task without punctuation syntax.
- `что я думал про аудит`, `помнишь идею про оффер`, `найди мою заметку про
  агентов` search personal Dusk first.
- Questions about learned material, an author or a term may additionally search
  Research Brain. Results are separated under `📚 Из агентских конспектов`.
- A question that has personal hits shows them first even when study hits also
  exist.

### Buttons

The persistent keyboard exposes user goals:

- `💭 Мысль`
- `🔎 Найти`
- `➕ Задача`
- `📋 Задачи`
- `🎓 Материал`
- `📊 Статус`

Health remains available as `/health`. Research Brain has no primary button.

### Voice

Voice is transcribed once, then passed through the same router as text. A pending
button mode wins: after `🔎 Найти`, voice becomes the query; after `➕ Задача`,
voice becomes the title; after `💭 Мысль`, voice is saved as a thought. With no
mode, the transcript is interpreted exactly like typed text.

The response includes the transcript so recognition mistakes are visible.

## Personal search

Search only these Dusk locations:

- `ZETA/FLEETING/00 Инбокс.md`
- `ZETA/FLEETING_DAILY/`
- `ZETA/PERMANENT Знание в СВОЙ инсайт/` and Unicode/path-compatible variants
- `TaskNotes/Tasks/`
- `PARA/PROJECTS/`
- `PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК/`
- `STICKY/` — временные заметки
- `Omi/` — память Omi
- `PARA/RESOURCES/` и `PARA/RESOURCES хобби и заметки по ним/` — ресурсы
- `DAILY/`
- root `Отложено.md`, `Статус.md`, `Недельный обзор.md`

The search is bounded by file count, file size, query length and result count.
It rejects symlinks outside the vault, binary/NUL data and invalid UTF-8. It
uses exact phrases, significant Russian/English words and short stems so the
user need not remember the exact form of each word. Result rendering includes
type, title, date/status when present, path and a short matching excerpt.

No index, network or model is required for recall.

## Research Brain routing

Research is secondary and read-only. It activates only when the query has an
explicit knowledge cue: a definitional/question phrase (`кто такой`, `что такое`,
`что означает`, `объясни концепт/термин`), a reported-knowledge question
(`что <автор> говорил/советовал`), an explicit find over knowledge material
(`найди/покажи … в конспектах/термин/материал`), or a direct Research Brain
question. A bare `термин`, `материал`, `советовал`, `рассказывал` or
`research brain` inside a plain statement does **not** activate it. It also does
not activate for `что я думал`, `моя идея`, `моя задача`, `я записывал` and
similar personal-memory wording, and personal intent always suppresses it.

## Data flow

1. Telegram authorizes the user.
2. Text or transcribed voice enters one assistant router.
3. A pending mode is handled first.
4. Otherwise the router chooses `personal_search`, `task`, `knowledge_search`,
   or safe default `capture`.
5. Mutations use existing exact task/note/inbox and neutral git-sync paths.
6. Reads do not mutate or sync.

## Error handling

- Failed transcription leaves no capture and reports a short retry message.
- Empty or overly broad search asks for a subject instead of dumping the vault.
- Search errors skip an unreadable file and continue.
- A search with no Dusk hit says so honestly; Research results remain labelled.
- Callback and git failures log exception types without user content or tokens.

## Verification

- RED tests first for natural routing, Dusk-first recall, Research isolation,
  voice-mode equivalence and removal of punctuation syntax from user-facing UI.
- Full existing `bot/test_*.py` suite.
- Target Python/Telegram runtime import and handler registration.
- Isolated smoke test with fixture vault: capture → recall → task, plus personal
  and study result separation.
- Live deploy only after independent review and clean-state/hash checks.

## Deferred

Timed Telegram reminders are a separate bounded phase. The current correction
builds reliable capture and recall first; it does not pretend that a stored
sentence is a scheduled notification.
