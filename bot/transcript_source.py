#!/usr/bin/env python3
"""Источник для проверяемого конспекта: сегменты, порции и проверка цитат.

Зачем отдельный модуль: конспект должен быть **проверяемым**. Значит нужны
(1) сохранённый исходный текст с фрагментами и таймкодами, (2) устойчивые ID
фрагментов, чтобы тезис можно было привязать к конкретному месту, (3) честная
проверка дословной цитаты, а не доверие модели.

Модуль намеренно без сети и без модели: всё, что здесь есть, — чистая работа с
текстом, поэтому проверяется синтетическими fixtures.

Правила, которые здесь зафиксированы:

* **ID фрагмента — это порядок разбора**, `seg-0001`… Разбор детерминирован,
  поэтому ID устойчивы между запусками.
* **Ничего не выдумываем.** Нет таймкода — так и помечаем. Нет автора, языка,
  названия — поле пустое, а не правдоподобное.
* **Дословная цитата** подтверждается только по исходному тексту. Допустимая
  нормализация ровно одна: сжатие пробелов и переносов строк в один пробел
  (и снятие обрамляющих кавычек). Регистр не подменяем.
* **Роллинг-субтитры** YouTube повторяют строку в следующем cue; повтор
  отрезается, а не склеивается в дубль.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field

CHUNK_CHARS = 12000
MAX_SEGMENT_CHARS = 2000
MIN_QUOTE_CHARS = 12
CONTIGUOUS_GAP = 0.5
MIN_OVERLAP_CHARS = 12
MIN_OVERLAP_WORDS = 2
DUPLICATE_MIN_CHARS = 20
NOTES_SECTION = "## Заметки"
DEFAULT_RAW_NAME_VTT = "original.vtt"
DEFAULT_RAW_NAME_TEXT = "original.txt"

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?$")
_UNKNOWN = "не известен"


def _stamp(seconds) -> str:
    """Секунды в читаемый таймкод HH:MM:SS."""
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _seconds(value: str):
    """Таймкод VTT в секунды. Непонятное значение — None, а не догадка."""
    match = _TIME.match((value or "").strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    secs = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + secs + millis / 1000


def _normalise(text: str) -> str:
    """Единственная допустимая нормализация: пробелы и переносы → один пробел."""
    return _WS.sub(" ", (text or "")).strip()


def _strip_wrapping_quotes(text: str) -> str:
    value = (text or "").strip()
    for left, right in (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'")):
        if len(value) >= 2 and value.startswith(left) and value.endswith(right):
            return value[1:-1].strip()
    return value


def _clean_cue_text(lines) -> str:
    parts = []
    for line in lines:
        line = _TAGS.sub("", line)
        line = line.replace("&nbsp;", " ").replace("&amp;", "&")
        line = line.replace("&lt;", "<").replace("&gt;", ">").strip()
        if line:
            parts.append(line)
    return _normalise(" ".join(parts))


def video_id(url: str) -> str:
    """ID видео из известных форм ссылки. Неизвестная форма — пустая строка."""
    url = (url or "").strip()
    for pattern in (r"[?&]v=([A-Za-z0-9_-]{6,})",
                    r"youtu\.be/([A-Za-z0-9_-]{6,})",
                    r"/shorts/([A-Za-z0-9_-]{6,})",
                    r"/embed/([A-Za-z0-9_-]{6,})",
                    r"/live/([A-Za-z0-9_-]{6,})"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def _is_word_end(text: str, index: int) -> bool:
    """Правая граница слова: конец строки или не буквенно-цифровой символ."""
    if index >= len(text):
        return True
    return not text[index].isalnum()


def _overlap_chars(previous: str, text: str) -> int:
    """Перекрытие по ЦЕЛЫМ словам: конец предыдущего == начало нового.

    Проверяется и правая граница: `language model` не считается перекрытием
    для `language modeling works well`, иначе от второго cue осталось бы
    `ing works well`. Совпадение по префиксу слова содержимое не удаляет.
    """
    prev_words, text_words = previous.split(), text.split()
    best = 0
    for size in range(1, min(len(prev_words), len(text_words)) + 1):
        tail = " ".join(prev_words[-size:])
        if not text.startswith(tail):
            continue
        if not _is_word_end(text, len(tail)):
            continue
        if len(tail) >= MIN_OVERLAP_CHARS and size >= MIN_OVERLAP_WORDS:
            best = len(tail)
    return best


def _dedupe_rolling(cues) -> list:
    """Убирает роллинг-повторы авто-субтитров — только при надёжных признаках.

    Признак роллинга: следующая реплика начинается там же, где закончилась
    предыдущая, и повторяет её хвост. Если времени нет или между репликами
    разрыв — текст сохраняется как есть: настоящий повтор (припев, цитата)
    удалять нельзя.
    """
    out = []
    for start, end, text in cues:
        if not text:
            continue
        if out:
            prev_start, prev_end, prev_text = out[-1]
            contiguous = (start is not None and prev_end is not None
                          and start <= prev_end + CONTIGUOUS_GAP)
            if contiguous:
                if len(text) >= DUPLICATE_MIN_CHARS and text in prev_text:
                    continue
                overlap = _overlap_chars(prev_text, text)
                if overlap:
                    fresh = text[overlap:].strip()
                    if not fresh:
                        continue
                    out.append((start, end, fresh))
                    continue
        out.append((start, end, text))
    return out


def _split_by_words(text: str, size: int) -> list:
    """Режет текст по границам слов, не теряя и не дублируя символы."""
    pieces, current = [], ""
    for word in text.split(" "):
        if current and len(current) + len(word) + 1 > size:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
        while len(current) > size:          # слово длиннее лимита — режем по символам
            pieces.append(current[:size])
            current = current[size:]
    if current:
        pieces.append(current)
    return pieces


def _split_long_cues(cues, size: int = MAX_SEGMENT_CHARS) -> list:
    """Длинная реплика становится несколькими фрагментами с тем же временем."""
    out = []
    for start, end, text in cues:
        if len(text) <= size:
            out.append((start, end, text))
            continue
        for piece in _split_by_words(text, size):
            out.append((start, end, piece))
    return out


def _segments_from_pieces(pieces, timed: bool) -> list:
    segments = []
    for index, piece in enumerate(pieces, start=1):
        start, end, text = piece if timed else (None, None, piece)
        segments.append(Segment(id=f"seg-{index:04d}", start=start, end=end, text=text))
    return segments


def parse_vtt(raw: str) -> list:
    """Разбирает VTT в сегменты. Таймкоды — только те, что реально были."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        stamp_line = next((line for line in lines if "-->" in line), None)
        if stamp_line is None:
            continue
        left, _, right = stamp_line.partition("-->")
        body = [line for line in lines
                if "-->" not in line
                and not line.strip().isdigit()
                and not line.strip().upper().startswith(
                    ("WEBVTT", "KIND:", "LANGUAGE:", "NOTE", "STYLE", "REGION"))]
        cue = _clean_cue_text(body)
        if cue:
            cues.append((_seconds(left), _seconds(right), cue))
    if cues:
        return _segments_from_pieces(_split_long_cues(_dedupe_rolling(cues)), timed=True)

    # Таймкодов нет вовсе: сохраняем текст как есть, но без выдуманного времени.
    plain = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        body = [line for line in lines
                if not line.strip().upper().startswith(
                    ("WEBVTT", "KIND:", "LANGUAGE:", "NOTE", "STYLE", "REGION"))]
        cue = _clean_cue_text(body)
        if cue:
            plain.append(cue)
    return _segments_from_pieces(_split_plain(" ".join(plain)), timed=False)


def _split_plain(text: str, size: int = 400) -> list:
    """Режет текст без таймкодов: по предложениям, а длинное — по словам."""
    text = _normalise(text)
    if not text:
        return []
    pieces = []
    for sentence in _SENTENCE.split(text):
        pieces.extend(_split_by_words(sentence, size))
    return pieces


@dataclass(frozen=True)
class Segment:
    id: str
    start: float | None
    end: float | None
    text: str

    @property
    def stamp(self):
        return _stamp(self.start) if self.start is not None else None

    def as_json(self) -> dict:
        return {"id": self.id, "start": self.start, "end": self.end,
                "stamp": self.stamp, "text": self.text}


@dataclass
class Source:
    url: str
    video_id: str = ""
    language: str = ""
    title: str = ""
    author: str = ""
    fetched_at: str = ""
    segments: list = field(default_factory=list)
    raw_name: str = DEFAULT_RAW_NAME_VTT
    raw_text: str = ""

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments).strip()

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def has_timecodes(self) -> bool:
        return any(segment.start is not None for segment in self.segments)

    def segment(self, segment_id: str):
        for item in self.segments:
            if item.id == segment_id:
                return item
        return None

    def stamp_of(self, segment_id: str):
        item = self.segment(segment_id)
        return item.stamp if item else None

    def start_of(self, segment_id: str):
        item = self.segment(segment_id)
        return item.start if item else None

    @property
    def key(self) -> str:
        """Имя папки источника: ID видео, иначе короткий хэш ссылки."""
        if self.video_id:
            return self.video_id
        return hashlib.sha1((self.url or "").encode("utf-8")).hexdigest()[:12]


def from_vtt(url: str, raw: str, *, language: str = "", title: str = "",
             author: str = "", fetched_at: str | None = None,
             raw_name: str = DEFAULT_RAW_NAME_VTT) -> Source:
    return Source(
        url=url, video_id=video_id(url), language=language, title=title,
        author=author,
        fetched_at=fetched_at or datetime.datetime.now().isoformat(timespec="seconds"),
        segments=parse_vtt(raw), raw_name=raw_name, raw_text=raw or "",
    )


def from_text(url: str, text: str, *, language: str = "", title: str = "",
              author: str = "", fetched_at: str | None = None) -> Source:
    """Источник без субтитров: текст сохраняется, таймкоды не выдумываются."""
    return Source(
        url=url, video_id=video_id(url), language=language, title=title,
        author=author,
        fetched_at=fetched_at or datetime.datetime.now().isoformat(timespec="seconds"),
        segments=_segments_from_pieces(_split_plain(text), timed=False),
        raw_name=DEFAULT_RAW_NAME_TEXT, raw_text=text or "",
    )


@dataclass(frozen=True)
class Chunk:
    id: str
    segment_ids: list
    text: str
    chars: int
    text_chars: int = 0


def chunks(source: Source, limit: int = CHUNK_CHARS) -> list:
    """Последовательные порции: весь текст, без пропуска середины.

    Служебная метка `[seg-XXXX]` входит в размер порции, но не в её текст:
    поэтому `text_chars` считает только знаки источника — по нему потом
    считается покрытие без двойного учёта.
    """
    if limit <= 0:
        raise ValueError("limit должен быть положительным")
    groups = []
    for segment in source.segments:
        label = f"[{segment.id}{' ' + segment.stamp if segment.stamp else ''}]"
        room = max(1, limit - len(label) - 1)
        bodies = [segment.text] if len(segment.text) <= room else _split_by_words(segment.text, room)
        for body in bodies:
            # Метка у каждой части: порция обязана показывать модели, из какого
            # фрагмента она собрана, иначе тезис не на что привязать.
            piece = f"{label} {body}"
            piece_chars = len(body)
            if groups and groups[-1]["chars"] + len(piece) + 1 > limit:
                groups.append({"ids": [], "pieces": [], "chars": 0, "text_chars": 0})
            if not groups:
                groups.append({"ids": [], "pieces": [], "chars": 0, "text_chars": 0})
            groups[-1]["ids"].append(segment.id)
            groups[-1]["pieces"].append(piece)
            groups[-1]["chars"] += len(piece) + 1
            groups[-1]["text_chars"] += piece_chars
    return [
        Chunk(id=f"chunk-{index:03d}", segment_ids=group["ids"],
              text="\n".join(group["pieces"]), chars=max(0, group["chars"] - 1),
              text_chars=group["text_chars"])
        for index, group in enumerate(groups, start=1)
    ]


def chunks_from_text(text: str, limit: int = CHUNK_CHARS) -> list:
    """Порции для текста без сегментов: режем последовательно, ничего не теряя."""
    text = text or ""
    if not text:
        return []
    pieces = [text[i:i + limit] for i in range(0, len(text), limit)]
    return [Chunk(id=f"chunk-{index:03d}", segment_ids=[], text=piece,
                  chars=len(piece), text_chars=len(piece))
            for index, piece in enumerate(pieces, start=1)]


@dataclass(frozen=True)
class QuoteCheck:
    found: bool
    segment_id: str | None = None
    matched: str = ""
    reason: str = ""


def find_quote(source: Source, quote: str) -> QuoteCheck:
    """Проверяет дословность по исходному тексту.

    Разрешена ровно одна нормализация — сжатие пробелов/переносов в один пробел
    и снятие обрамляющих кавычек. Совпадение по смыслу цитатой не считается;
    слишком короткая строка доказательством не является.
    """
    target = _normalise(_strip_wrapping_quotes(quote))
    if len(target) < MIN_QUOTE_CHARS:
        return QuoteCheck(False, None, "", "слишком короткая строка для доказательства")

    joined = _normalise(source.text)
    position = joined.find(target)
    if position == -1:
        return QuoteCheck(False, None, "", "в исходном тексте такой фразы нет")

    offset, segment_id = 0, None
    for segment in source.segments:
        piece = _normalise(segment.text)
        if offset <= position < offset + len(piece) + 1:
            segment_id = segment.id
            break
        offset += len(piece) + 1
    return QuoteCheck(True, segment_id, joined[position:position + len(target)])


def segments_of_text(source: Source, needle: str) -> list:
    """ID фрагментов, в которых встречается фраза (нормализованно)."""
    target = _normalise(needle)
    if not target:
        return []
    return [segment.id for segment in source.segments
            if target in _normalise(segment.text)]


class Transcript(str):
    """Текст для модели, несущий рядом структурированный источник.

    Строка остаётся строкой (совместимо со старыми вызовами `_distill`), а
    `source` доступен там, где нужны фрагменты и проверка цитат.
    """

    source: Source | None = None

    def __new__(cls, text: str, source: Source | None = None):
        obj = super().__new__(cls, text)
        obj.source = source
        return obj


def as_transcript(source: Source) -> Transcript:
    return Transcript(source.text, source)


def to_json(source: Source) -> dict:
    return {
        "url": source.url,
        "video_id": source.video_id,
        "language": source.language,
        "title": source.title,
        "author": source.author,
        "fetched_at": source.fetched_at,
        "raw_name": source.raw_name,
        "segment_count": len(source.segments),
        "has_timecodes": source.has_timecodes,
        "segments": [segment.as_json() for segment in source.segments],
    }


def render_markdown(source: Source) -> str:
    """Человекочитаемая страница источника со ссылками на фрагменты."""
    lines = [
        f"# Источник: {source.title or 'название ' + _UNKNOWN}",
        "",
        f"- Ссылка: {source.url}",
        f"- ID видео: {source.video_id or _UNKNOWN}",
        f"- Язык: {source.language or _UNKNOWN}",
        f"- Автор/канал: {source.author or _UNKNOWN}",
        f"- Получено: {source.fetched_at or _UNKNOWN}",
        f"- Фрагментов: {len(source.segments)}",
        "",
        "## Фрагменты",
        "",
    ]
    # Каждый фрагмент — заголовок: ссылка вида `source.md#seg-0001` ведёт на
    # реальную цель (https://obsidian.md/help/links), а не на жирный текст.
    for segment in source.segments:
        stamp = segment.stamp if segment.stamp else "таймкод недоступен"
        lines += [f"### {segment.id}", "", f"`{stamp}` — {segment.text}", ""]
    lines += ["", NOTES_SECTION, ""]
    return "\n".join(lines) + "\n"


def immutable_part(page: str) -> str:
    """Часть страницы источника без изменяемого раздела заметок."""
    return page.split(NOTES_SECTION, 1)[0]


def source_from_json(data: dict) -> Source:
    """Собирает источник обратно из JSON — для детерминированной сверки с MD."""
    segments = [
        Segment(id=str(item["id"]), start=item.get("start"), end=item.get("end"),
                text=str(item["text"]))
        for item in data["segments"]
    ]
    return Source(
        url=str(data.get("url") or ""), video_id=str(data.get("video_id") or ""),
        language=str(data.get("language") or ""), title=str(data.get("title") or ""),
        author=str(data.get("author") or ""), fetched_at=str(data.get("fetched_at") or ""),
        segments=segments, raw_name=str(data.get("raw_name") or DEFAULT_RAW_NAME_TEXT),
        raw_text="",
    )
