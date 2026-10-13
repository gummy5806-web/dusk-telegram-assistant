"""Immutable, local history for edits of public thoughts and resources.

The journal is deliberately separate from current source files.  A source edit
is recorded as ``prepared -> source write -> committed``; an interrupted or
failed source write therefore cannot be presented as a committed revision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import queue_store

HISTORY_REL = "SYSTEM/MEMORY_HISTORY"
MAX_CONTENT_BYTES = 512 * 1024
MAX_EVENTS = 100
_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_KINDS = frozenset(("thought", "resource"))


class HistoryError(ValueError):
    """History cannot safely be recorded or read."""


@dataclass(frozen=True)
class SupersedeMutation:
    ok: bool
    message: str
    mutated: bool = False
    stale: bool = False
    proposal_id: str = ""


def prepare_change(vault, kind: str, ref: str, before_identity: str,
                   before_content: str, after_identity: str,
                   after_content: str) -> str:
    """Durably prepare a source mutation and return its immutable journal id."""
    root = Path(vault)
    _validate_change(kind, ref, before_identity, before_content, after_identity, after_content)
    event_id = _new_id()
    path = _event_path(root, event_id, "prepared", create=True)
    payload = {
        "id": event_id,
        "kind": kind,
        "ref": ref,
        "created_at": _now(),
        "before": _snapshot(before_identity, before_content),
        "after": _snapshot(after_identity, after_content),
    }
    queue_store.atomic_write(path, _json(payload))
    return event_id


def commit_change(vault, event_id: str) -> None:
    """Add a commit marker only after the corresponding source write succeeded."""
    root = Path(vault)
    prepared = _event_path(root, event_id, "prepared", create=False)
    if not prepared.exists() or prepared.is_symlink():
        raise HistoryError("История изменения недоступна.")
    marker = _event_path(root, event_id, "committed", create=True)
    if marker.exists():
        return
    queue_store.atomic_write(marker, _json({"id": event_id, "committed_at": _now()}))


def abort_change(vault, event_id: str) -> None:
    """Mark a prepared mutation as aborted when its source write did not happen."""
    try:
        root = Path(vault)
        prepared = _event_path(root, event_id, "prepared", create=False)
        if not prepared.exists() or prepared.is_symlink():
            return
        marker = _event_path(root, event_id, "aborted", create=True)
        if not marker.exists():
            queue_store.atomic_write(marker, _json({"id": event_id, "aborted_at": _now()}))
    except (HistoryError, OSError):
        # This is a best-effort diagnostic marker.  The missing committed marker
        # remains the authoritative guarantee.
        return


def history(vault, kind: str, ref: str, limit: int = 10) -> list[dict]:
    """Return committed versions for one public current or historical reference."""
    if kind not in _KINDS or not isinstance(ref, str) or not ref:
        return []
    events = _committed_events(Path(vault))
    identities = _connected_identities(ref, events) if kind == "thought" else {ref}
    matched = [event for event in events if event["kind"] == kind
               and _matches_ref(event, identities)]
    matched.sort(key=lambda event: (event["created_at"], event["id"]), reverse=True)
    bounded = max(1, min(int(limit) if isinstance(limit, int) else 10, MAX_EVENTS))
    return [_public_event(Path(vault), event) for event in matched[:bounded]]


def prepare_supersede(vault, older_identity: str, newer_identity: str) -> dict:
    """Create an explicit, human-confirmable proposal without changing thoughts."""
    root = Path(vault)
    older = _current_thought(root, older_identity)
    newer = _current_thought(root, newer_identity)
    if older is None or newer is None or older_identity == newer_identity:
        raise HistoryError("Мысль изменилась — выбери обе записи заново.")
    if _would_cycle(root, older_identity, newer_identity):
        raise HistoryError("Эта связь создаёт цикл истории мыслей.")
    proposal_id = _new_id()
    proposal = {
        "id": proposal_id,
        "status": "prepared",
        "created_at": _now(),
        "older_identity": older_identity,
        "newer_identity": newer_identity,
        "older_hash": _sha256(_thought_content(root, older)),
        "newer_hash": _sha256(_thought_content(root, newer)),
    }
    queue_store.atomic_write(_proposal_path(root, proposal_id, create=True), _json(proposal))
    return dict(proposal)


def proposal(vault, proposal_id: str) -> dict | None:
    """Read one proposal for a confirmation view; never infers a relationship."""
    value = _read_json(_proposal_path(Path(vault), proposal_id, create=False))
    return dict(value) if _valid_proposal(value, proposal_id) else None


def commit_supersede(vault, proposal_id: str) -> SupersedeMutation:
    """Confirm exactly one prepared proposal if both exact identities are current."""
    root = Path(vault)
    try:
        path = _proposal_path(root, proposal_id, create=False)
    except HistoryError:
        return SupersedeMutation(False, "⚠️ Подтверждение устарело.", stale=True)
    try:
        domain_lock = _supersede_lock_path(root)
    except HistoryError:
        return SupersedeMutation(False, "⚠️ Подтверждение устарело.", stale=True)
    with queue_store.queue_lock(domain_lock):
        value = _read_json(path)
        if not _valid_proposal(value, proposal_id) or value["status"] != "prepared":
            return SupersedeMutation(False, "⚠️ Подтверждение уже использовано или устарело.", stale=True)
        if not _proposal_current(root, value):
            return SupersedeMutation(False, "⚠️ Одна из мыслей изменилась — выбери их заново.", stale=True)
        if _would_cycle(root, value["older_identity"], value["newer_identity"]):
            return SupersedeMutation(False, "⚠️ Эта связь создаёт цикл истории мыслей.", stale=True)
        relation = {
            "id": proposal_id,
            "older_identity": value["older_identity"],
            "newer_identity": value["newer_identity"],
            "older_hash": value["older_hash"],
            "newer_hash": value["newer_hash"],
            "created_at": _now(),
            "active": True,
        }
        relation_path = _relation_path(root, proposal_id, create=True)
        existing = _read_json(relation_path) if relation_path.exists() else None
        if relation_path.exists() and existing is None:
            return SupersedeMutation(False, "⚠️ Подтверждение уже использовано или устарело.", stale=True)
        if existing is not None and not _same_relation(existing, relation):
            return SupersedeMutation(False, "⚠️ Подтверждение уже использовано или устарело.", stale=True)
        if _has_other_replacement(root, relation):
            return SupersedeMutation(False, "⚠️ Эта мысль уже заменена другой текущей мыслью.", stale=True)
        try:
            if existing is None:
                queue_store.atomic_write(relation_path, _json(relation))
            value["status"] = "committed"
            value["committed_at"] = _now()
            queue_store.atomic_write(path, _json(value))
        except OSError:
            return SupersedeMutation(False, "⚠️ Не удалось подтвердить связь мыслей.")
    return SupersedeMutation(True, "Прошлая мысль отмечена как заменённая.", True,
                             proposal_id=proposal_id)


def restore_current(vault, proposal_id: str) -> SupersedeMutation:
    """Revoke a supersede relation while retaining every version and proposal."""
    root = Path(vault)
    try:
        path = _relation_path(root, proposal_id, create=False)
    except HistoryError:
        return SupersedeMutation(False, "⚠️ Восстановление устарело.", stale=True)
    try:
        domain_lock = _supersede_lock_path(root)
    except HistoryError:
        return SupersedeMutation(False, "⚠️ Восстановление устарело.", stale=True)
    with queue_store.queue_lock(domain_lock):
        relation = _read_json(path)
        if not _valid_relation(relation, proposal_id) or not relation["active"]:
            return SupersedeMutation(False, "⚠️ Восстановление устарело.", stale=True)
        if not _relation_effective(root, relation):
            return SupersedeMutation(False, "⚠️ Восстановление устарело.", stale=True)
        if not _relation_current(root, relation):
            return SupersedeMutation(False, "⚠️ Мысль изменилась — связь не снята.", stale=True)
        relation["active"] = False
        relation["restored_at"] = _now()
        try:
            queue_store.atomic_write(path, _json(relation))
        except OSError:
            return SupersedeMutation(False, "⚠️ Не удалось снять связь мыслей.")
    return SupersedeMutation(True, "Мысль снова считается текущей.", True,
                             proposal_id=proposal_id)


def is_past(vault, identity: str) -> bool:
    """Whether an explicit active supersede relation marks this thought as past."""
    return past_source(vault, identity) is not None


def past_source(vault, identity: str) -> dict | None:
    """Return the explicit relation responsible for a past designation, if any."""
    if not isinstance(identity, str) or not identity:
        return None
    root = Path(vault)
    resolved = resolve_identity(root, identity)
    for relation in _effective_relations(root):
        if resolve_identity(root, relation["older_identity"]) == resolved:
            return dict(relation)
    return None


def resolve_identity(vault, identity: str) -> str:
    """Follow committed edit links so old Inbox and structured identities remain useful."""
    if not isinstance(identity, str) or not identity:
        return ""
    current = identity
    seen = {current}
    events = sorted(_committed_events(Path(vault)), key=lambda event: (event["created_at"], event["id"]))
    while True:
        next_values = [event["after"]["identity"] for event in events
                       if event["kind"] == "thought" and event["before"]["identity"] == current]
        if not next_values or next_values[-1] in seen:
            return current
        current = next_values[-1]
        seen.add(current)


def _validate_change(kind, ref, before_identity, before_content, after_identity, after_content):
    if kind not in _KINDS or not all(isinstance(value, str) and value for value in
                                     (ref, before_identity, before_content, after_identity, after_content)):
        raise HistoryError("История изменения недоступна.")
    if any(len(value.encode("utf-8")) > MAX_CONTENT_BYTES
           for value in (before_content, after_content)):
        raise HistoryError("Изменение слишком большое для безопасной истории.")
    if kind == "resource" and not _ID_RE.fullmatch(ref):
        raise HistoryError("История изменения недоступна.")


def _snapshot(identity: str, content: str) -> dict:
    return {"identity": identity, "hash": _sha256(content), "content": content,
            "timestamp": _now()}


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _new_id() -> str:
    return secrets.token_hex(12)


def _history_dir(root: Path, create: bool) -> Path:
    path = root / HISTORY_REL
    if create:
        current = root
        for part in Path(HISTORY_REL).parts:
            current = current / part
            if current.is_symlink():
                raise HistoryError("История изменения недоступна.")
            current.mkdir(exist_ok=True)
    if path.is_symlink() or not path.is_dir() or not _inside(root, path):
        raise HistoryError("История изменения недоступна.")
    return path


def _event_path(root: Path, event_id: str, state: str, create: bool) -> Path:
    if not _ID_RE.fullmatch(str(event_id)) or state not in {"prepared", "committed", "aborted"}:
        raise HistoryError("История изменения недоступна.")
    directory = _history_dir(root, create)
    path = directory / "changes" / f"{event_id}.{state}.json"
    if create:
        path.parent.mkdir(exist_ok=True)
    if path.parent.is_symlink() or not _inside(root, path.parent):
        raise HistoryError("История изменения недоступна.")
    return path


def _proposal_path(root: Path, proposal_id: str, create: bool) -> Path:
    return _named_path(root, "proposals", proposal_id, create)


def _relation_path(root: Path, proposal_id: str, create: bool) -> Path:
    return _named_path(root, "relations", proposal_id, create)


def _named_path(root: Path, directory_name: str, identifier: str, create: bool) -> Path:
    if not _ID_RE.fullmatch(str(identifier)):
        raise HistoryError("История изменения недоступна.")
    directory = _history_dir(root, create) / directory_name
    if directory.is_symlink():
        raise HistoryError("История изменения недоступна.")
    if create:
        directory.mkdir(exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or not _inside(root, directory):
        raise HistoryError("История изменения недоступна.")
    return directory / f"{identifier}.json"


def _supersede_lock_path(root: Path) -> Path:
    """A fixed, safe lock target serializing every relationship transition."""
    directory = _history_dir(root, create=False)
    path = directory / ".supersede-domain.lock"
    if path.is_symlink() or not _inside(root, path.parent):
        raise HistoryError("История изменения недоступна.")
    return path


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _read_json(path: Path):
    try:
        if path.is_symlink() or path.stat().st_size > MAX_CONTENT_BYTES * 3:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _committed_events(root: Path) -> list[dict]:
    try:
        directory = _history_dir(root, False) / "changes"
        if directory.is_symlink() or not directory.is_dir() or not _inside(root, directory):
            return []
    except HistoryError:
        return []
    events = []
    for path in directory.glob("*.prepared.json"):
        event_id = path.name.removesuffix(".prepared.json")
        if not _ID_RE.fullmatch(event_id):
            continue
        if not (directory / f"{event_id}.committed.json").is_file():
            continue
        value = _read_json(path)
        if _valid_event(value, event_id):
            events.append(value)
    return events


def _valid_event(value, event_id: str) -> bool:
    return (isinstance(value, dict) and value.get("id") == event_id and value.get("kind") in _KINDS
            and isinstance(value.get("ref"), str) and isinstance(value.get("created_at"), str)
            and _valid_snapshot(value.get("before")) and _valid_snapshot(value.get("after")))


def _valid_snapshot(value) -> bool:
    return (isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key]
                                            for key in ("identity", "hash", "content"))
            and len(value["content"].encode("utf-8")) <= MAX_CONTENT_BYTES
            and value["hash"] == _sha256(value["content"]))


def _matches_ref(event: dict, identities: set[str]) -> bool:
    if event["kind"] == "resource":
        return event["ref"] in identities
    return (event["before"]["identity"] in identities
            or event["after"]["identity"] in identities)


def _connected_identities(identity: str, events: list[dict]) -> set[str]:
    """Find one immutable edit chain in either direction for an old/current ref."""
    known = {identity}
    while True:
        expanded = set(known)
        for event in events:
            if event["kind"] != "thought":
                continue
            before = event["before"]["identity"]
            after = event["after"]["identity"]
            if before in known or after in known:
                expanded.update((before, after))
        if expanded == known:
            return known
        known = expanded


def _public_event(root: Path, event: dict) -> dict:
    value = json.loads(json.dumps(event, ensure_ascii=False))
    value["status"] = "committed"
    marker = _read_json(_event_path(root, event["id"], "committed", create=False))
    value["committed_at"] = marker.get("committed_at", "") if isinstance(marker, dict) else ""
    value["before"]["is_current"] = _is_current(root, event["kind"], event["before"])
    value["after"]["is_current"] = _is_current(root, event["kind"], event["after"])
    return value


def _is_current(root: Path, kind: str, snapshot: dict) -> bool:
    if kind == "thought":
        return _current_thought(root, snapshot["identity"]) is not None
    try:
        import resources
        record = resources.load(root, snapshot["identity"])
        if record is None:
            return False
        return _sha256((root / record.path).read_text(encoding="utf-8")) == snapshot["hash"]
    except (ImportError, OSError, UnicodeDecodeError):
        return False


def _current_thought(root: Path, identity: str):
    try:
        import thought_feed
        return thought_feed.load_thought(root, identity)
    except ImportError:
        return None


def _thought_content(root: Path, thought) -> str:
    if thought.kind == "inbox":
        path = root / thought.path
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if thought.identity.endswith("#" + hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]):
                return line
        raise HistoryError("Мысль изменилась — выбери её заново.")
    return (root / thought.path).read_text(encoding="utf-8")


def _valid_proposal(value, proposal_id) -> bool:
    return (isinstance(value, dict) and value.get("id") == proposal_id
            and value.get("status") in {"prepared", "committed"}
            and all(isinstance(value.get(key), str) and value[key] for key in
                    ("older_identity", "newer_identity", "older_hash", "newer_hash", "created_at")))


def _proposal_current(root: Path, value: dict) -> bool:
    older = _current_thought(root, value["older_identity"])
    newer = _current_thought(root, value["newer_identity"])
    return (older is not None and newer is not None
            and _sha256(_thought_content(root, older)) == value["older_hash"]
            and _sha256(_thought_content(root, newer)) == value["newer_hash"])


def _valid_relation(value, proposal_id) -> bool:
    return (isinstance(value, dict) and value.get("id") == proposal_id
            and isinstance(value.get("active"), bool)
            and all(isinstance(value.get(key), str) and value[key] for key in
                    ("older_identity", "newer_identity", "older_hash", "newer_hash", "created_at")))


def _relation_current(root: Path, relation: dict) -> bool:
    return _proposal_current(root, relation)


def _same_relation(existing: dict, expected: dict) -> bool:
    return (_valid_relation(existing, expected["id"])
            and all(existing.get(key) == expected[key] for key in
                    ("id", "older_identity", "newer_identity", "older_hash", "newer_hash")))


def _effective_relations(root: Path) -> list[dict]:
    """Only a matching committed proposal makes a persisted relation active."""
    effective = []
    for relation in _relations(root):
        if not relation["active"]:
            continue
        if _relation_effective(root, relation):
            effective.append(relation)
    return effective


def _relation_effective(root: Path, relation: dict) -> bool:
    record = proposal(root, relation["id"])
    return (record is not None and record.get("status") == "committed"
            and _same_relation(relation, record))


def _would_cycle(root: Path, older_identity: str, newer_identity: str) -> bool:
    """Reject an edge when the active directed supersede graph reaches back."""
    start = resolve_identity(root, older_identity)
    target = resolve_identity(root, newer_identity)
    graph: dict[str, set[str]] = {}
    for relation in _effective_relations(root):
        old = resolve_identity(root, relation["older_identity"])
        new = resolve_identity(root, relation["newer_identity"])
        graph.setdefault(old, set()).add(new)
    pending = [target]
    seen = set()
    while pending:
        value = pending.pop()
        if value == start:
            return True
        if value not in seen:
            seen.add(value)
            pending.extend(graph.get(value, ()))
    return False


def _has_other_replacement(root: Path, candidate: dict) -> bool:
    """One exact old thought may have only one active current successor."""
    old = resolve_identity(root, candidate["older_identity"])
    for relation in _effective_relations(root):
        if relation["id"] == candidate["id"]:
            continue
        if resolve_identity(root, relation["older_identity"]) == old:
            return True
    return False


def _relations(root: Path) -> list[dict]:
    try:
        directory = _history_dir(root, False) / "relations"
        if directory.is_symlink() or not directory.is_dir() or not _inside(root, directory):
            return []
    except HistoryError:
        return []
    values = []
    for path in directory.glob("*.json"):
        value = _read_json(path)
        if _valid_relation(value, path.stem):
            values.append(value)
    return values
