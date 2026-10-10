"""Synced resolution state for exact public thought identities; no note content."""
from __future__ import annotations
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
import inbox_core

STATUS_REL = Path('SYSTEM/THOUGHT_STATUS')
_LOCK = threading.RLock()
logger = logging.getLogger(__name__)

class StatusError(ValueError):
    """Resolution state cannot be safely read or changed."""


def _path(vault, identity: str, create: bool = False) -> Path | None:
    if not isinstance(identity, str) or len(identity) > 2048 or not re.fullmatch(
            r'(?:ZETA/FLEETING/00 Инбокс\.md|ZETA/FLEETING_DAILY/[^/]+\.md)#[a-f0-9]{12}', identity):
        raise StatusError('invalid public thought identity')
    root = Path(vault)
    if root.is_symlink() or not root.is_dir():
        raise StatusError('unsafe vault')
    directory = root
    for part in STATUS_REL.parts:
        directory = directory / part
        if directory.is_symlink():
            raise StatusError('unsafe status directory')
        if not directory.exists():
            if not create:
                return None
            directory.mkdir(exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise StatusError('unsafe status directory')
    path = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.json')
    if path.is_symlink():
        raise StatusError('unsafe status file')
    return path


def _read(path: Path | None, identity: str) -> bool:
    if path is None or not path.exists():
        return False
    try:
        if path.stat().st_size > 4096:
            raise StatusError('oversized status')
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        raise StatusError('unreadable status') from error
    if (not isinstance(record, dict) or set(record) != {'identity', 'done'}
            or record['identity'] != identity or type(record['done']) is not bool):
        raise StatusError('invalid status record')
    return record['done']


def get_done(vault, identity: str) -> bool:
    """Strict read for displaying an actual resolution state."""
    try:
        return _read(_path(vault, identity), identity)
    except OSError as error:
        raise StatusError('could not read thought status') from error


def is_done(vault, identity: str) -> bool:
    """Malformed/unsafe state is withheld from active suggestions, never reset."""
    try:
        return get_done(vault, identity)
    except (OSError, StatusError) as error:
        logger.warning('thought status unavailable: %s', type(error).__name__)
        return True


def set_done(vault, identity: str, done: bool) -> bool:
    """Atomically record a resolution/reopening; return whether state changed."""
    if type(done) is not bool:
        raise StatusError('done must be boolean')
    with _LOCK:
        try:
            path = _path(vault, identity, create=True)
            with inbox_core.locked_write(path):
                _path(vault, identity)
                if _read(path, identity) == done:
                    return False
                inbox_core.atomic_write(path, json.dumps({'identity': identity, 'done': done},
                                                        ensure_ascii=False) + '\n')
            return True
        except (OSError, StatusError) as error:
            raise StatusError('could not update thought status') from error
