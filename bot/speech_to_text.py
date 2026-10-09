"""Bounded REST client for the local rotating Deepgram proxy."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_PROXY_URL = "http://127.0.0.1:8321/v1/listen"
DEFAULT_MODEL = "nova-3"
DEFAULT_LANGUAGE = "multi"
DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TranscriptionError(RuntimeError):
    """The proxy request or Deepgram response could not produce a transcript."""


def _proxy_url(value: str | None) -> str:
    raw = (value if value is not None else
           os.getenv("DEEPGRAM_PROXY_URL", DEFAULT_PROXY_URL)).strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TranscriptionError("invalid Deepgram proxy URL")
    if parsed.username or parsed.password:
        raise TranscriptionError("Deepgram proxy URL must not contain credentials")
    return raw


def _timeout_seconds(value: float | None) -> float:
    if value is not None:
        timeout = float(value)
    else:
        raw = os.getenv("DEEPGRAM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw)
        except ValueError as error:
            raise TranscriptionError("invalid Deepgram timeout") from error
    if not 1.0 <= timeout <= 180.0:
        raise TranscriptionError("Deepgram timeout is outside 1..180 seconds")
    return timeout


def _request_url(base_url: str, model: str, language: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({
        "model": model,
        "language": language,
        "smart_format": "true",
    })
    return urllib.parse.urlunsplit(parsed._replace(
        query=urllib.parse.urlencode(query)))


def _extract_transcript(payload: object) -> str:
    try:
        transcript = payload["results"]["channels"][0]["alternatives"][0][
            "transcript"]
    except (KeyError, IndexError, TypeError) as error:
        raise TranscriptionError("malformed Deepgram response") from error
    if not isinstance(transcript, str):
        raise TranscriptionError("malformed Deepgram transcript")
    return transcript.strip()


def transcribe_file(
    path: str | os.PathLike[str],
    *,
    proxy_url: str | None = None,
    client_key: str | None = None,
    model: str | None = None,
    language: str | None = None,
    timeout: float | None = None,
) -> str:
    """Send one audio file to the configured proxy and return plain text."""
    audio_path = Path(path)
    try:
        size = audio_path.stat().st_size
    except OSError as error:
        raise TranscriptionError("audio file is unavailable") from error
    if size <= 0:
        raise TranscriptionError("audio file is empty")
    if size > MAX_AUDIO_BYTES:
        raise TranscriptionError("audio file is too large")

    try:
        audio = audio_path.read_bytes()
    except OSError as error:
        raise TranscriptionError("audio file could not be read") from error

    resolved_model = (model or os.getenv("DEEPGRAM_MODEL", DEFAULT_MODEL)).strip()
    resolved_language = (
        language or os.getenv("DEEPGRAM_LANGUAGE", DEFAULT_LANGUAGE)).strip()
    if not resolved_model or not resolved_language:
        raise TranscriptionError("Deepgram model and language are required")

    url = _request_url(
        _proxy_url(proxy_url), resolved_model, resolved_language)
    mime_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    headers = {
        "Accept": "application/json",
        "Content-Type": mime_type,
        "User-Agent": "Dusk-Telegram-Bot/1.0",
    }
    resolved_key = (client_key if client_key is not None else
                    os.getenv("DEEPGRAM_PROXY_CLIENT_KEY", "")).strip()
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"

    request = urllib.request.Request(
        url, data=audio, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
                request, timeout=_timeout_seconds(timeout)) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise TranscriptionError(
            f"Deepgram proxy returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TranscriptionError("Deepgram proxy is unavailable") from error

    if len(body) > MAX_RESPONSE_BYTES:
        raise TranscriptionError("Deepgram response is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranscriptionError("invalid Deepgram JSON response") from error
    return _extract_transcript(payload)
