#!/usr/bin/env python3
"""Единая точка вызова модели.

ПРАВИЛО ПРИВАТНОСТИ (задано владельцем валта):
    Личный контент — мысли, брайндампы, задачи — обрабатывается ТОЛЬКО своим
    Vertex в своём GCP-проекте. Авторизация берётся из gcloud (ADC).
    Сторонние API для личного НЕ используются, даже если удобнее или дешевле.

Поэтому по умолчанию provider=vertex, и он работает только там, где есть
gcloud + SDK — то есть на Mac. На VPS разбор не запускается сознательно.

Второй провайдер (openai-совместимый) оставлен для рабочего контента и
включается явно через LLM_PROVIDER, чтобы случайно не улетело личное.

Запускать интерпретатором, у которого есть SDK:
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import ai_usage

DEFAULT_VERTEX_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"
LOGGER = logging.getLogger(__name__)


def _project() -> str:
    """Проект: сначала переменные, потом файл ADC, и только потом gcloud.

    Порядок важен: под launchd у процесса урезанный PATH и gcloud не находится,
    поэтому запуск по таймеру падал с "Не знаю GCP-проект", хотя из терминала
    всё работало. Файл ADC читается всегда и от PATH не зависит.
    """
    project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if project:
        return project

    # ADC: authorized_user кладёт проект в quota_project_id / project_id
    adc = Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
               or Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
    try:
        data = json.loads(adc.read_text(encoding="utf-8"))
        for key in ("quota_project_id", "project_id"):
            if data.get(key):
                return data[key]
    except Exception:
        pass

    for candidate in ("gcloud", "/opt/homebrew/bin/gcloud", "/usr/local/bin/gcloud",
                      "/usr/bin/gcloud"):
        try:
            out = subprocess.run([candidate, "config", "get-value", "project"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            if out and "unset" not in out.lower():
                return out
        except Exception:
            continue
    return ""


def _usage_count(metadata, name: str) -> int:
    """Read a non-negative SDK usage counter without retaining model content."""
    value = metadata.get(name) if isinstance(metadata, dict) else getattr(metadata, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _record_attempt(*, provider: str, model: str, call_id: str, response=None,
                    status: str, error_type: str | None = None) -> None:
    """Best-effort local accounting; a full ledger must never block generation."""
    metadata = (response.get("usage_metadata") if isinstance(response, dict)
                else getattr(response, "usage_metadata", None)) if response is not None else None
    candidates = _usage_count(metadata, "candidates_token_count")
    thoughts = _usage_count(metadata, "thoughts_token_count")
    try:
        ai_usage.record_call(
            provider=provider, model=model, kind="generation", call_id=call_id,
            input_tokens=_usage_count(metadata, "prompt_token_count"),
            # Gemini reports candidates and thoughts independently; its total
            # includes prompt + candidates + thoughts.  Keep both billable
            # output categories so neither one is silently undercounted.
            output_tokens=candidates, thinking_tokens=thoughts,
            estimated=metadata is None, status=status, error_type=error_type,
        )
    except Exception as error:
        LOGGER.warning("AI usage ledger unavailable: %s", type(error).__name__)


def _vertex(prompt: str, model: str, *, response_schema: dict | None = None) -> str:
    call_id = uuid.uuid4().hex
    try:
        from google import genai
    except ImportError as error:
        _record_attempt(provider="vertex", model=model, call_id=call_id,
                        status="error", error_type=type(error).__name__)
        return ("Vertex SDK недоступен в этом интерпретаторе. Разбор личного "
                "запускается только на Mac: /Library/Frameworks/Python.framework/"
                "Versions/3.13/bin/python3")

    project = _project()
    if not project:
        _record_attempt(provider="vertex", model=model, call_id=call_id,
                        status="error", error_type="ConfigurationError")
        return "Не знаю GCP-проект: задай VERTEX_PROJECT или выполни gcloud config set project"

    location = os.getenv("VERTEX_LOCATION") or DEFAULT_LOCATION
    client = None
    try:
        client = genai.Client(vertexai=True, project=project, location=location)
        kwargs = {}
        if response_schema is not None:
            from google.genai import types
            kwargs["config"] = types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=response_schema)
        response = client.models.generate_content(model=model, contents=prompt, **kwargs)
        _record_attempt(provider="vertex", model=model, call_id=call_id,
                        response=response, status="ok")
        text = getattr(response, "text", None)
        if text:
            return text
        # у «думающих» моделей текст может не собраться при малом лимите
        return json.dumps({"error": "пустой ответ модели"})
    except Exception as e:
        _record_attempt(provider="vertex", model=model, call_id=call_id,
                        status="error", error_type=type(e).__name__)
        return f"Ошибка Vertex ({type(e).__name__}): {str(e)[:200]}"
    finally:
        # google-genai exposes ``close`` on clients that own transport resources.
        # Detection keeps compatibility with older SDK releases and test doubles.
        if client is not None:
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception as error:
                LOGGER.warning("Vertex client close failed: %s", type(error).__name__)


def _openai_compatible(prompt: str, model: str) -> str:
    call_id = uuid.uuid4().hex
    base = (os.getenv("LLM_BASE_URL") or "").rstrip("/")
    key = os.getenv("LLM_API_KEY") or ""
    if not base or not key:
        _record_attempt(provider="openai-compatible", model=model, call_id=call_id,
                        status="error", error_type="ConfigurationError")
        return "LLM_BASE_URL/LLM_API_KEY не заданы — провайдер недоступен"

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = json.loads(r.read())
        # Legacy compatible endpoints do not promise the Vertex metadata shape;
        # they are therefore recorded as estimated and normally unpriced.
        _record_attempt(provider="openai-compatible", model=model, call_id=call_id,
                        response=payload, status="ok")
        return payload["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        _record_attempt(provider="openai-compatible", model=model, call_id=call_id,
                        status="error", error_type=type(e).__name__)
        return f"Ошибка модели HTTP {e.code}: {e.read()[:200].decode('utf-8', 'ignore')}"
    except Exception as e:
        _record_attempt(provider="openai-compatible", model=model, call_id=call_id,
                        status="error", error_type=type(e).__name__)
        return f"Ошибка модели ({type(e).__name__}): {str(e)[:200]}"


def ask(prompt: str, provider: str | None = None, model: str | None = None,
        *, response_schema: dict | None = None) -> str:
    provider = (provider or os.getenv("LLM_PROVIDER") or "vertex").lower()
    if provider == "vertex":
        if response_schema is not None:
            return _vertex(prompt, model or os.getenv("LLM_MODEL") or DEFAULT_VERTEX_MODEL,
                           response_schema=response_schema)
        return _vertex(prompt, model or os.getenv("LLM_MODEL") or DEFAULT_VERTEX_MODEL)
    if response_schema is not None:
        raise ValueError("structured output requires Vertex")
    return _openai_compatible(prompt, model or os.getenv("LLM_MODEL") or "qwen-plus")
