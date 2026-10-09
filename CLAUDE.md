# Dusk Vault — Claude Code Context

## Vault Structure (PARA + Zettelkasten)

### PARA
- `PARA/PROJECTS/` — Active projects (type: project_family, project_note)
- `PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК/` — Long-term development areas (type: area_family, area_note)
- `PARA/RESOURCES хобби и заметки по ним/` — Resources, docs, meetings, contacts
- `PARA/ARCHIVES/` — Completed/paused items
- `PARA/WORKSTATION/` — Work setup notes

### ZETA (Zettelkasten)
- `ZETA/FLEETING/` — Quick thoughts (type: fleeting_note)
- `ZETA/FLEETING_DAILY/` — Daily fleeting captures
- `ZETA/LITERATURE Изучение,конспект/` — Learning notes from sources (type: literature_note)
- `ZETA/PERMANENT Знание в СВОЙ инсайт/` — Synthesized knowledge (type: permanent_note)

### Knowledge Graph (NEW — Claude Code extended memory)
- `knowledge/decisions/` — Architectural and business decisions with reasoning (type: decision)
- `knowledge/patterns/` — Confirmed code/workflow patterns (type: pattern)
- `knowledge/debugging/` — Bugs found and how they were solved (type: debug_note)
- `sessions/` — Session logs from Claude Code work (type: session_log)

### Other
- `DAILY/DAILY/` — Daily notes (YYYY-MM-DD format)
- `HUB/` — Homepage, Map of Contents, Mail Box
- `SYSTEM/TEMPLATE/FORMAT/` — All note templates
- `Flashcards/` — Spaced repetition cards
- `Priority_Matrix-main/` — Eisenhower matrix system
- `1 HabitStreakRPG/` — Gamified habit tracker

## Template Frontmatter (MUST follow exactly)

### Fleeting Note
Location: ZETA/FLEETING/
```yaml
connections:
tags: [fleeting_note]
type: fleeting_note
created: YYYY-MM-DDTHH:mm
```

### Literature Note
Location: ZETA/LITERATURE Изучение,конспект/
```yaml
connections:
reference:
tags: [literature_note]
type: literature_note
created: YYYY-MM-DDTHH:mm
```

### Permanent Note
Location: ZETA/PERMANENT Знание в СВОЙ инсайт/
```yaml
connections:
tags: [permanent_note]
type: permanent_note
created: YYYY-MM-DDTHH:mm
```

### Project
Location: PARA/PROJECTS/
```yaml
Priority_Level: # "1 Critical", "2 High", "3 Medium", "4 Low"
Status: # "1 To Do", "2 In Progress", "3 Testing", "4 Completed", "5 Blocked"
Date_Created:
Due_Date:
connections:
tags: [project]
type: project_family
cssclasses: [hide-properties_editing, hide-properties_reading]
```

### Area Family
Location: PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК/
```yaml
area: <folder_name>
area_category:
summary:
tags: []
type: area_family
created: YYYY-MM-DDTHH:mm
```

### Area Note
```yaml
area: <folder_name>
summary:
tags: []
type: area_note
created: YYYY-MM-DDTHH:mm
```

### Priority Matrix Task
Location: Priority_Matrix-main/PriorityMatrix/
```yaml
eisenhower_status: # urgent_important | not_urgent_important | urgent_not_important | not_urgent_not_important
due_date: YYYY-MM-DD
created: YYYY-MM-DDTHH:mm
tags: [priority_matrix]
```

### Meeting
Location: PARA/RESOURCES хобби и заметки по ним/MEETINGS/
```yaml
scheduled_date:
start_time:
end_time:
summary: ""
meeting_status: false
tags: [meeting]
type: meeting
created: YYYY-MM-DDTHH:mm
cssclasses: [hide-properties_editing, hide-properties_reading]
```

### Contact
Location: PARA/RESOURCES хобби и заметки по ним/CONTACTS/
```yaml
company:
location:
title:
email:
phone: 0
aliases:
tags: []
type: contact
```

### Documentation Note
Location: PARA/RESOURCES хобби и заметки по ним/DOCUMENTATIONS/
```yaml
connections: []
reference:
tags: [documentation_note]
type: documentation_note
created: YYYY-MM-DDTHH:mm
```

### Decision Note
Location: knowledge/decisions/
```yaml
connections:
tags: [decision]
type: decision
context:
alternatives:
created: YYYY-MM-DDTHH:mm
```
Naming: use prose-as-title — "выбрали Supabase Auth вместо NextAuth из-за RLS.md", NOT "auth-decision.md"

### Pattern Note
Location: knowledge/patterns/
```yaml
connections:
tags: [pattern]
type: pattern
applies_to:
created: YYYY-MM-DDTHH:mm
```

### Debug Note
Location: knowledge/debugging/
```yaml
connections:
tags: [debug_note]
type: debug_note
symptoms:
root_cause:
created: YYYY-MM-DDTHH:mm
```

### Session Log
Location: sessions/
```yaml
tags: [session_log]
type: session_log
project:
duration:
created: YYYY-MM-DDTHH:mm
```

## Tag Conventions
- Projects: `#project/{folder_name_snake_case}`
- Areas: `#area/{area_name}/{note_name}`
- Contacts: `#contact/{name}`
- Priority: `#urgent-important`, `#schedule`, `#delegate`, `#eliminate`

## Inbox / Mail Box System

The vault uses HUB/Mail Box.md as the primary inbox dashboard (DataCore JSX).
- Shows incomplete tasks, unchecked items, unattended meetings
- Queries ALL folders EXCEPT: SYSTEM, HUB, DAILY
- New captures from Telegram go to ZETA/FLEETING/ as fleeting_note type
- They automatically appear in Mail Box and Homepage dashboards
- DO NOT create an inbox/ folder — it's not integrated with dashboards

## Rules
- NEVER modify existing notes unless explicitly asked
- ALWAYS use the exact frontmatter format above when creating notes
- ALWAYS place notes in the correct folder based on type
- When creating Priority Matrix tasks, use eisenhower_status field
- When linking notes, use Obsidian wikilinks: `[[Note Name]]`
- Attachments go in `SYSTEM/MEDIA/`
- Daily notes use template from `SYSTEM/TEMPLATE/FORMAT/Lite Daily.md`
- Telegram bot captures go to ZETA/FLEETING/ (NOT inbox/)
- All notes in ZETA/FLEETING/ appear automatically in Mail Box and Homepage

## Session Rhythm (Orient → Work → Persist)

### При старте сессии (Orient)
- Прочитай этот CLAUDE.md
- Если задача касается конкретной темы — поищи в knowledge/ и ZETA/PERMANENT/ релевантные заметки
- Проверь sessions/ — последний лог покажет что делали в прошлый раз

### Во время работы (Work)
- Если принял архитектурное решение или выбирал между вариантами — запомни, чтобы записать
- Если нашёл и починил баг — запомни причину и решение
- Если заметил повторяющийся паттерн — запомни

### При завершении (Persist) — по команде "сохрани сессию" или "persist"
1. Создай лог в `sessions/YYYY-MM-DD тема сессии.md` — что делали, что решили, что осталось
2. Если были решения → создай заметку в `knowledge/decisions/` (prose-as-title)
3. Если были баги → создай заметку в `knowledge/debugging/` (prose-as-title)
4. Если нашёл паттерн → создай заметку в `knowledge/patterns/`
5. Если изучал тему глубоко → создай в `ZETA/PERMANENT Знание в СВОЙ инсайт/`
6. Добавь wiki-ссылки `[[имя заметки]]` между связанными заметками
7. Используй правильный frontmatter из шаблонов выше

### Правило именования (prose-as-title)
Называй заметки утверждениями, не категориями:
- BAD: `auth.md`, `bugs.md`, `database.md`
- GOOD: `выбрали Supabase Auth вместо NextAuth из-за RLS.md`
- GOOD: `rate limit ChatPlace 500 запросов потом 429 на 5 минут.md`
- GOOD: `event_date ставится триггером из created_at автоматически.md`
