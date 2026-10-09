"""Shared deterministic privacy policy for capture and background triage."""

import re

PRIVATE_PATTERNS = (
    r"\bврач", r"\bанализ", r"\bбольниц", r"\bздоров", r"\bболезн", r"\bлечен",
    r"\bтаблетк", r"\bтерапевт", r"\bпсихо", r"\bдепресс", r"\bтревог", r"\bдиагноз", r"\bсимптом",
    r"\bзарплат", r"\bдолг", r"\bкредит", r"\bипотек", r"\bналог", r"\bбухгалтер",
    r"\bжен[аеуыо]", r"\bжених", r"\bдевушк", r"\bпарн[яею]",
    r"\bразвод", r"\bсемь[яиюе]", r"\bотношен", r"\bлюбов",
    r"\bдневник", r"\bинтим", r"\bсекс", r"\bпаспорт", r"\bпароль",
)


def looks_private(text: str) -> bool:
    return any(re.search(pattern, (text or "").lower()) for pattern in PRIVATE_PATTERNS)
