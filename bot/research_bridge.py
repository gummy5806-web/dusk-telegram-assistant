#!/usr/bin/env python3
"""Read-only мост между Dusk и Research Brain с проверяемым происхождением.

Что делает. По вопросу собирает **evidence records**: короткие фрагменты
размеченных материалов research-brain (`raw/`, `wiki/`, `links/`) и
подтверждённых stage-2B применений Dusk. Для каждого показывает путь, заголовок,
точные 1-based строки, слой происхождения, явные typed-relations из блока
`## Связи Dusk` и наблюдения пользователя.

Чего здесь нет и быть не должно:

* записи, индекса, БД, копии или миграции — только чтение Markdown;
* LLM, сети, `subprocess`, git, API, Obsidian URI и создания задач;
* семантических выводов о конфликте, истинности или авторитете.

Содержимое заметок — недоверенные данные. Строки вида «ignore previous
instructions», HTML и команды не исполняются и не влияют на управление: в ответе
это лишь ограниченная цитата с указанием происхождения.

Публичные функции: `search`, `diagnostics`, `dusk_root`, `research_root`,
`detect_research_root`, `parse_relations`, `base_layer`, `layer_for`,
`domain_for`, `query_terms`, `safe_text`, `walk_markdown`. Все пути проверяются
по `resolve()` и обязаны оставаться внутри разрешённого root.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

import frontmatter
import learning_cycle

# ── границы и лимиты ───────────────────────────────────────────────────────

RESEARCH_SUBDIRS = ("raw", "wiki", "links")
DUSK_SUBDIRS = (learning_cycle.TASKS_DIR,)

SKIP_DIRS = {".git", ".obsidian", ".trash", ".smart-env", ".space",
             ".obsidian-mcp", "node_modules", "Private", "SYSTEM", "sessions",
             "Users", "Flashcards"}

MAX_KEYWORDS = 12
MAX_RESULTS = 12
MAX_FRAGMENT = 240
MAX_TITLE = 120
MAX_QUERY_CHARS = 500
MAX_HEADING = 120
MAX_PATH = 240
MAX_DOMAIN = 60
MAX_OBSERVATION = 200
MAX_CONCLUSION = 200
MAX_USER_TEXT = 200
MAX_TARGET = 240
MAX_FILE_BYTES = 200_000
MIN_SCORE = 2

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или
ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где
есть надо нужно это этап этот эти для при про без над под мой моя мои твой
какой какая какие почему зачем что-то кто-то""".split())

_AUTHOR_HEADINGS = {
    "рекомендация автора", "авторская рекомендация", "мнение автора",
    "author summary", "author_summary",
}
_DUSK_HEADINGS = {
    "интерпретация dusk", "интерпретация", "dusk interpretation",
    "dusk_interpretation",
}

REL_TYPES = {
    "supports": "supports", "подтверждает": "supports", "поддерживает": "supports",
    "contradicts": "contradicts", "противоречит": "contradicts", "опровергает": "contradicts",
    "similar": "similar", "похоже": "similar", "похожий": "similar",
    "application": "application", "применение": "application",
    "result": "result", "результат": "result",
}

_REL_LINE = re.compile(
    r"^-\s*(?:\*\*)?(?P<type>[A-Za-zА-Яа-яёЁ_]+)(?:\*\*)?\s*:\s*(?P<target>.+?)\s*$")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
# Structural ATX heading: до 3 ведущих пробелов, 1-6 `#`, затем текст.
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
# Fence opener/close: до 3 ведущих пробелов, 3+ `` ` `` или `~`, хвост не важен.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?previous|disregard\s+(all\s+)?(previous|prior)|"
    r"system\s*prompt|<script|</?\w+[^>]*>|^\s*(?:rm|curl|wget|sudo)\s)",
    re.IGNORECASE)


# ── корни ──────────────────────────────────────────────────────────────────

def dusk_root(vault: Path | None = None) -> Path:
    """Корень Dusk: явный аргумент или `VAULT_PATH`, иначе `~/dusk-vault`."""
    if vault is not None:
        return Path(vault)
    return Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def detect_research_root(vault: Path | None = None) -> Path:
    """Корень Research Brain: env, затем сосед Dusk, затем известный путь."""
    env = os.getenv("RESEARCH_BRAIN_PATH")
    if env:
        return Path(env)
    main = dusk_root(vault)
    for cand in (main.parent / "research-brain",
                 Path.home() / "Documents/obsidian/research-brain"):
        if cand.is_dir():
            return cand
    return main.parent / "research-brain"


def research_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else detect_research_root()


# ── безопасное чтение ──────────────────────────────────────────────────────

def _inside(path: Path, root: Path) -> bool:
    """После `resolve()` путь остаётся внутри root (symlink наружу отсекается)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def safe_text(path: Path, root: Path):
    """`(текст, причина)`. Причина пустая — файл прочитан и безопасен.

    Проверяются: существование, расширение, размер, отсутствие NUL-байтов,
    UTF-8 и containment по `resolve()` (symlink наружу не читается).
    """
    path = Path(path)
    try:
        if not path.is_file():
            return None, "не файл"
        if path.suffix.lower() != ".md":
            return None, "не Markdown"
        if not _inside(path, root):
            return None, "вне разрешённого root (symlink?)"
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, f"слишком большой файл ({path.stat().st_size} байт)"
        raw = path.read_bytes()
        if b"\x00" in raw:
            return None, "бинарный файл (NUL)"
        return raw.decode("utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"не читается: {type(exc).__name__}"


def _in_scope(rel: str, allowed) -> bool:
    return any(rel == a or rel.startswith(a + "/") for a in allowed)


def walk_markdown(root: Path, vault: str, allowed):
    """Безопасный обход `allowed`-подкаталогов. `(files, diagnostics)`.

    `files` — список `(path, rel_posix, text)`; `diagnostics` — причины пропуска.
    Симлинк-каталоги не обходятся, symlink-файлы наружу отсекаются по `resolve()`.
    """
    root = Path(root)
    files, diag = [], []
    if not root.is_dir():
        return files, diag
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
            and not (base / d).is_symlink())
        for name in sorted(filenames):
            path = base / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if not _in_scope(rel, allowed):
                continue
            if not name.lower().endswith(".md"):
                diag.append({"vault": vault, "path": rel, "reason": "не Markdown"})
                continue
            text, reason = safe_text(path, root)
            if text is None:
                diag.append({"vault": vault, "path": rel, "reason": reason})
                continue
            files.append((path, rel, text))
    return files, diag


# ── разбор метаданных и слоёв ──────────────────────────────────────────────

def _meta(text: str) -> dict:
    def get(name: str) -> str:
        return frontmatter.get(text, name) or ""
    return {
        "type": get("type").strip(),
        "author": get("author").strip(),
        "domain": get("domain").strip(),
        "source": get("source").strip(),
        "dusk_id": get("dusk_id").strip(),
        "author_summary": get("author_summary").strip(),
        "dusk_interpretation": get("dusk_interpretation").strip(),
    }


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes", "да")


def base_layer(rel: str, meta: dict) -> str:
    """Слой по структуре пути и `type`. Неизвестное — `unknown`, без догадок."""
    typ = (meta.get("type") or "").lower()
    if rel.startswith("raw/"):
        return "source"
    if rel.startswith("links/") or "/links/" in rel:
        return "source"
    if "/sources/" in rel:
        return "source"
    if typ in ("source", "source_collection", "link"):
        return "source"
    if "/concepts/" in rel or "/analysis/" in rel or "/entities/" in rel:
        return "dusk_interpretation"
    if typ in ("concept", "analysis", "entity", "synthesis", "analysis_note"):
        return "dusk_interpretation"
    return "unknown"


def layer_for(rel: str, meta: dict, heading: str, forced: str = "") -> str:
    """Итоговый слой фрагмента: явные metadata/heading важнее структуры.

    `forced` задаётся мостом только для подтверждённой пары stage-2B и никогда
    не выводится из текста.
    """
    if forced:
        return forced
    h = (heading or "").strip().lower()
    if h in _AUTHOR_HEADINGS:
        return "author_summary"
    if h in _DUSK_HEADINGS:
        return "dusk_interpretation"
    if _truthy(meta.get("author_summary", "")) or meta.get("type", "").lower() == "author_summary":
        return "author_summary"
    if _truthy(meta.get("dusk_interpretation", "")):
        return "dusk_interpretation"
    return base_layer(rel, meta)


def domain_for(meta: dict) -> str:
    """Домен только из явных metadata, не из текста и не по совпадению слов.

    Авторитет не меняет evidence layer, score или истинность — это лишь метка.
    """
    explicit = (meta.get("domain") or "").strip().lower()
    if explicit:
        return explicit
    author = (meta.get("author") or "").strip().lower()
    source = (meta.get("source") or "").strip().lower()
    blob = f"{author} {source}"
    if "hormozi" in blob:
        return "sales/marketing"
    if "karpathy" in blob:
        return "ai/agents"
    return ""


# ── relations ──────────────────────────────────────────────────────────────

def _structure(lines):
    """Единый скан Markdown-структуры: `(headings, in_fence)`.

    Markdown heading/fence/relations начинаются только после корректно
    закрытого leading YAML frontmatter. `headings` — `(index, level, text)` для
    настоящих ATX-заголовков вне code fence. `in_fence[i]` — строка внутри
    fenced-блока. Закрыть текущий fence может только строка с тем же
    fence-символом и не короче открывающего.
    """
    start = _body_start(lines)
    headings = []
    in_fence = [False] * len(lines)
    fence_char, fence_len = None, 0
    for i in range(start, len(lines)):
        line = lines[i]
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            char, tail = marker[0], match.group(2)
            if fence_char is None:
                fence_char, fence_len = char, len(marker)
            elif char == fence_char and len(marker) >= fence_len and not tail.strip():
                fence_char, fence_len = None, 0
            in_fence[i] = True
            continue
        if fence_char is not None:
            in_fence[i] = True
            continue
        heading = _ATX.match(line)
        if heading:
            text = (heading.group(2) or "").strip().rstrip("#").strip()
            headings.append((i, len(heading.group(1)), text))
    return headings, in_fence


def parse_relations(text: str) -> list:
    """Typed relations только из явного блока `## Связи Dusk`.

    Блок уровня 2 заканчивается на следующем structural heading равного или
    более высокого уровня (`#`/`##`) вне code fence; `###` его не закрывает.
    Блок внутри ```/~~~ — цитата/пример, а не разметка связей. Обычный `Related`
    и keyword overlap relations не создают.
    """
    lines = text.splitlines()
    headings, in_fence = _structure(lines)
    start, level = None, 2
    for index, hlevel, htext in headings:
        if hlevel == 2 and htext.lower() == "связи dusk":
            start, level = index, hlevel
            break
    if start is None:
        return []
    end = len(lines)
    for index, hlevel, _text in headings:
        if index > start and hlevel <= level:
            end = index
            break
    out = []
    for i in range(start + 1, end):
        if in_fence[i]:
            continue
        match = _REL_LINE.match(lines[i].strip())
        if not match:
            continue
        kind = REL_TYPES.get(match.group("type").strip().lower())
        if not kind:
            continue
        out.append({"type": kind, "target": match.group("target").strip()})
    return out


def _safe_target_path(raw: str):
    r"""`(ok, norm)` для vault-relative POSIX path target relation.

    Отклоняются: пустой путь, абсолютный префикс, Windows drive (`C:`), UNC
    (`\\server\share`), NUL и компоненты `.`/`..`. Обратные слеши считаются
    разделителями при проверке, но опасный префикс не очищается до валидного
    пути. `id:<dusk_id>` — отдельная ветка и сюда не попадает.
    """
    if not raw or "\x00" in raw:
        return False, ""
    norm = raw.replace("\\", "/")
    if norm.startswith("/"):
        return False, ""                      # absolute / UNC после нормализации
    if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
        return False, ""                      # Windows drive: C:/...
    parts = norm.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False, ""
    return True, norm


class _Index:
    """In-memory карта для разрешения relations; ничего не пишет на диск."""

    def __init__(self):
        self.ids: dict = {}
        self.paths: dict = {}
        self.by_identity: dict = {}
        self.stable: dict = {}        # vault:rel -> стабильная identity
        self.stable_paths: dict = {}  # стабильная identity -> rel

    def mark_stable(self, vault: str, rel: str, identity: str) -> None:
        self.stable[f"{vault}:{rel}"] = identity
        self.stable_paths[identity] = rel

    def _stable(self, ident: str) -> str:
        return self.stable.get(ident, ident)

    def add(self, vault: str, rel: str, text: str) -> str:
        ident = f"{vault}:{rel}"
        noext = rel[:-3] if rel.endswith(".md") else rel
        self.paths[f"{vault}:{noext}"] = ident
        self.paths[f"{vault}:{rel}"] = ident
        self.by_identity[ident] = {"vault": vault, "rel": rel}
        did = _meta(text).get("dusk_id", "")
        if did:
            self.ids.setdefault(did, set()).add(ident)
        return ident

    def identity_for(self, vault: str, rel: str, text: str) -> str:
        """Стабильная identity: уникальный `dusk_id`, иначе vault + путь."""
        did = _meta(text).get("dusk_id", "")
        if did:
            known = self.ids.get(did, set())
            if len(known) == 1 and f"{vault}:{rel}" in known:
                return f"dusk_id:{did}"
        return f"{vault}:{rel}"

    def resolve(self, target: str, current_vault: str) -> dict:
        """Разрешает цель relation. `id:<dusk_id>` переживает rename файла."""
        raw = (target or "").strip()
        link = _WIKILINK.search(raw)
        if link:
            raw = link.group(1)
        raw = raw.split("|")[0].split("#")[0].strip()
        if raw.lower().startswith("id:"):
            key = raw[3:].strip()
            found = self.ids.get(key, set())
            if len(found) == 1:
                return {"state": "resolved", "target_id": self._stable(next(iter(found)))}
            if len(found) > 1:
                return {"state": "ambiguous", "target_id": "", "reason": "дубликат dusk_id"}
            return {"state": "missing", "target_id": "", "reason": "dusk_id не найден"}
        ok, norm = _safe_target_path(raw)
        if not ok:
            return {"state": "missing", "target_id": "",
                    "reason": "небезопасный путь relation"}
        if norm.endswith(".md"):
            norm = norm[:-3]
        for cand in (f"{current_vault}:{norm}", f"research-brain:{norm}", f"dusk:{norm}"):
            if cand in self.paths:
                return {"state": "resolved", "target_id": self._stable(self.paths[cand])}
        base = norm.split("/")[-1]
        matches = sorted({self._stable(ident) for key, ident in self.paths.items()
                          if key.split(":", 1)[-1].split("/")[-1] == base})
        if len(matches) == 1:
            return {"state": "resolved", "target_id": matches[0]}
        if len(matches) > 1:
            return {"state": "ambiguous", "target_id": "", "reason": "несколько совпадений"}
        return {"state": "missing", "target_id": "", "reason": "путь не найден"}

    def path_of(self, identity: str) -> str:
        if identity in self.stable_paths:
            return self.stable_paths[identity]
        info = self.by_identity.get(identity)
        return info["rel"] if info else identity


# ── поиск ──────────────────────────────────────────────────────────────────

def query_terms(query: str):
    """Детерминированные keywords/stems. Слова влияют только на retrieval score."""
    query = (query or "")[:MAX_QUERY_CHARS]      # явный bound до regex
    words = re.findall(r"[а-яёa-z0-9]{3,}", query.lower())
    keys, seen = [], set()
    for word in words:
        if word in STOP or word in seen:
            continue
        seen.add(word)
        keys.append(word)
        if len(keys) >= MAX_KEYWORDS:
            break
    stems = [k[:6] for k in keys]
    return keys, stems


def _body_start(lines) -> int:
    """Индекс первой строки тела после закрытого leading YAML frontmatter.

    Нет leading `---` → 0. Есть закрывающий `---` → сразу за ним.
    Незакрытый leading frontmatter → `len(lines)`: структуру из шапки не берём
    (fail closed). `---` в середине тела frontmatter не открывает.
    """
    if lines and lines[0].rstrip() == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip() == "---":
                return i + 1
        return len(lines)
    return 0


def _best_line(lines, keys, stems, start=0) -> tuple:
    best_i, best = None, 0
    for i in range(start, len(lines)):
        low = lines[i].lower()
        score = 0
        for key in keys:
            score += low.count(key)
        for stem in stems:
            score += low.count(stem)
        if score > best:
            best_i, best = i, score
    return best_i, best


def _score(text: str, title: str, keys, stems) -> tuple:
    low_title = (title or "").lower()
    title_score = sum(5 for key in keys if key in low_title)
    lines = text.splitlines()
    best_i, best = _best_line(lines, keys, stems, _body_start(lines))
    if best == 0:
        # совпадение только в шапке/заголовке файла — цитируем хотя бы его
        best_i, best = _best_line(lines, keys, stems, 0)
    return title_score + best, best_i


def _heading_at(lines, index) -> str:
    """Последний настоящий structural heading вне fence на позиции <= index."""
    if index is None:
        return ""
    headings, _in_fence = _structure(lines)
    found = ""
    for h_index, _level, text in headings:
        if h_index <= index:
            found = text
        else:
            break
    return found


def _clean_fragment(value: str) -> str:
    return _strip_control(value)[:MAX_FRAGMENT]


def _strip_control(value: str) -> str:
    """Снимает HTML/Markdown/wiki-control и схлопывает в одну строку.

    Содержимое заметок — недоверенные данные: теги, управляющие символы
    разметки и разметка вики-ссылок не должны попадать в Telegram как активная
    разметка.
    """
    text = re.sub(r"<[^>]*>", " ", value or "")
    text = _WIKILINK.sub(lambda match: match.group(1).replace("|", " "), text)
    text = re.sub(r"[`*_>#\[\]<>|]", " ", text)
    return " ".join(text.split())


def _clean_text(value: str, limit: int) -> str:
    """Одна безопасная строка с явным лимитом; усечение помечается `…`."""
    text = _strip_control(value)
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _bound(value: str, limit: int) -> str:
    """Служебный bound для identity/путей: `_`, `/`, `:` сохраняются."""
    text = (value or "").replace("<", " ").replace(">", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _is_untrusted(value: str) -> bool:
    return bool(_INJECTION.search(value or ""))


def _has_backlink(vault: Path, row: dict) -> bool:
    """Служебный backlink «конспект → задача» публичным правилом learning_cycle."""
    rel = row.get("source_rel") or ""
    if not rel:
        return False
    text, _reason = safe_text(Path(vault) / rel, Path(vault))
    if text is None:
        return False
    try:
        return learning_cycle.has_note_block(text, row.get("task_rel") or "")
    except (ValueError, OSError):
        return False


def _item(vault: str, rel: str, text: str) -> dict:
    return {"vault": vault, "rel": rel, "text": text,
            "forced_layer": "", "observations": []}


def _collect(dusk: Path, research: Path, today):
    """Собирает items/index/diagnostics/conflict edges. Только чтение."""
    index = _Index()
    items = []
    diag = []

    if research.is_dir():
        files, d = walk_markdown(research, "research-brain", RESEARCH_SUBDIRS)
        diag += d
        for _path, rel, text in files:
            index.add("research-brain", rel, text)
            items.append(_item("research-brain", rel, text))

    task_files, d = walk_markdown(dusk, "dusk", DUSK_SUBDIRS)
    diag += d
    # Сначала безопасно прочитанные карточки — во временную карту. В index/items
    # попадёт только та, которую подтверждают pair identity, source и backlink.
    by_rel = {rel: text for _path, rel, text in task_files}

    try:
        rows = learning_cycle.application_rows(dusk, today=today)["rows"]
    except (OSError, ValueError):
        rows = []
    for row in rows:
        if not row.get("source_ok"):
            continue                       # только подтверждённая пара 2Б
        rel = row.get("task_rel") or ""
        text = by_rel.get(rel)
        if text is None:
            continue                       # symlink/нечитаемая карточка не проходит
        if not _has_backlink(dusk, row):
            continue                       # pair/source/backlink — все три
        index.add("dusk", rel, text)
        item = _item("dusk", rel, text)
        item["forced_layer"] = "user_observation"
        observed = row["observed"] if row["has_observation"] else learning_cycle.NO_DATA
        conclusion = row["conclusion"] if row["has_conclusion"] else learning_cycle.NO_DATA
        item["observations"].append({
            "done": bool(row["done"]),
            "status": _clean_text(row.get("status", ""), MAX_USER_TEXT),
            "completed": _clean_text(row.get("completed", ""), MAX_USER_TEXT),
            "future": bool(row.get("future")),
            "undated": bool(row.get("undated")),
            "observed": _clean_text(observed, MAX_OBSERVATION),
            "conclusion": _clean_text(conclusion, MAX_CONCLUSION),
            "has_observation": bool(row["has_observation"]),
            "has_conclusion": bool(row["has_conclusion"]),
            "source": _clean_text(row.get("source_link") or "", MAX_PATH),
        })
        items.append(item)

    for item in items:
        item["identity"] = index.identity_for(item["vault"], item["rel"], item["text"])
        index.mark_stable(item["vault"], item["rel"], item["identity"])
        item["meta"] = _meta(item["text"])
        item["title"] = _clean_text(frontmatter.get(item["text"], "title")
                                     or Path(item["rel"]).stem, MAX_TITLE)

    edges = []
    for item in items:
        for rel in parse_relations(item["text"]):
            if rel["type"] != "contradicts":
                continue
            resolved = index.resolve(rel["target"], item["vault"])
            if resolved["state"] == "resolved" and resolved["target_id"]:
                edges.append((item["identity"], resolved["target_id"]))
    return items, index, diag, edges


def _conflicts_for(record_id: str, edges, index) -> list:
    """Конфликт — только явная `contradicts` в любую сторону."""
    out, seen = [], set()
    for src, tgt in edges:
        if src == record_id and tgt:
            key = ("outgoing", tgt)
            if key not in seen:
                seen.add(key)
                out.append({"direction": "outgoing", "id": tgt,
                            "path": index.path_of(tgt)})
        elif tgt == record_id and src:
            key = ("incoming", src)
            if key not in seen:
                seen.add(key)
                out.append({"direction": "incoming", "id": src,
                            "path": index.path_of(src)})
    out.sort(key=lambda c: (c["direction"], c["path"]))
    return out


def _record(item, index, line_i, score, edges):
    lines = item["text"].splitlines()
    if line_i is None or not lines:
        line_i = 0
    raw_line = lines[line_i] if lines else ""
    heading = _clean_text(_heading_at(lines, line_i if lines else None), MAX_HEADING)
    fragment = _clean_fragment(raw_line)
    layer = layer_for(item["rel"], item["meta"], heading, item["forced_layer"])
    relations = []
    for rel in parse_relations(item["text"]):
        resolved = index.resolve(rel["target"], item["vault"])
        target_path = (index.path_of(resolved["target_id"])
                       if resolved.get("target_id") else "")
        relations.append({
            "type": rel["type"],
            "target": _bound(rel["target"], MAX_TARGET),
            "state": resolved["state"],
            "target_id": _bound(resolved.get("target_id", ""), MAX_PATH),
            "target_path": _bound(target_path, MAX_PATH),
            "reason": _bound(resolved.get("reason", ""), MAX_USER_TEXT),
        })
    return {
        "id": item["identity"],
        "vault": item["vault"],
        "path": item["rel"],
        "title": item["title"],
        "heading": heading,
        "line_start": line_i + 1,
        "line_end": line_i + 1,
        "fragment": fragment,
        "excerpt": fragment,
        "score": score,
        "layer": layer,
        "relations": relations,
        "observations": item["observations"],
        "domain": _clean_text(domain_for(item["meta"]), MAX_DOMAIN),
        "untrusted": _is_untrusted(raw_line),
        "conflicts": _conflicts_for(item["identity"], edges, index),
    }


def search(query: str, limit: int = MAX_RESULTS, *, dusk: Path | None = None,
           research: Path | None = None, today: datetime.date | None = None) -> list:
    """Детерминированный поиск evidence по размеченной базе (только чтение)."""
    keys, stems = query_terms(query)
    if not keys:
        return []
    try:
        limit = max(0, min(int(limit), MAX_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_RESULTS
    dusk_path = Path(dusk) if dusk is not None else dusk_root()
    research_path = Path(research) if research is not None else research_root()
    items, index, _diag, edges = _collect(dusk_path, research_path, today)

    results = []
    for item in items:
        score, line_i = _score(item["text"], item["title"], keys, stems)
        if score < MIN_SCORE:
            continue
        results.append(_record(item, index, line_i, score, edges))
    results.sort(key=lambda r: (-r["score"], r["vault"], r["path"], r["line_start"]))
    return results[:limit] if limit else []


def diagnostics(*, dusk: Path | None = None, research: Path | None = None) -> list:
    """Безопасные причины пропуска файлов (symlink/размер/бинарность/чтение)."""
    dusk_path = Path(dusk) if dusk is not None else dusk_root()
    research_path = Path(research) if research is not None else research_root()
    out = []
    _files, d = walk_markdown(research_path, "research-brain", RESEARCH_SUBDIRS)
    out += d
    _files, d = walk_markdown(dusk_path, "dusk", DUSK_SUBDIRS)
    out += d
    return out
