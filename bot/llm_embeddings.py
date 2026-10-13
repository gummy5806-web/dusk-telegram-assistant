"""Personal retrieval embeddings, through the owner's existing Vertex project."""
from __future__ import annotations
import logging
import math
import os
import llm

MODEL = 'gemini-embedding-001'
DIMENSIONS = 768
MAX_TEXT_CHARS = 4000
logger = logging.getLogger(__name__)

class EmbeddingError(ValueError):
    """Safe public error without model input or provider secrets."""

def _record_usage(**fields):
    try:
        import ai_usage
        ai_usage.record_call(**fields)
    except Exception as error:
        logger.warning('embedding usage accounting unavailable: %s', type(error).__name__)


def embed(text: str, task_type: str) -> list[float]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise EmbeddingError('Слишком длинный или пустой запрос смыслового поиска.')
    if task_type not in {'RETRIEVAL_DOCUMENT', 'RETRIEVAL_QUERY'}:
        raise EmbeddingError('Неподдерживаемый вид смыслового поиска.')
    client = None
    status = 'error'
    tokens = 0
    characters = 0
    estimated = True
    try:
        from google import genai
        from google.genai.types import EmbedContentConfig
        project = llm._project()
        if not project:
            raise EmbeddingError('Vertex для смыслового поиска пока недоступен.')
        client = genai.Client(vertexai=True, project=project,
                              location=os.getenv('VERTEX_LOCATION') or llm.DEFAULT_LOCATION)
        response = client.models.embed_content(
            model=MODEL, contents=text,
            config=EmbedContentConfig(task_type=task_type, output_dimensionality=DIMENSIONS,
                                      auto_truncate=False))
        embeddings = getattr(response, 'embeddings', None)
        if not embeddings or len(embeddings) != 1:
            raise EmbeddingError('Смысловой поиск вернул некорректный результат.')
        raw = getattr(embeddings[0], 'values', None)
        if not isinstance(raw, (list, tuple)) or not raw or len(raw) > 3072:
            raise EmbeddingError('Смысловой поиск вернул некорректный результат.')
        vector = [float(value) for value in raw]
        if any(not math.isfinite(value) for value in vector) or not any(vector):
            raise EmbeddingError('Смысловой поиск вернул некорректный результат.')
        statistics = getattr(embeddings[0], 'statistics', None)
        count = getattr(statistics, 'token_count', None)
        if isinstance(count, (int, float)) and math.isfinite(count) and count >= 0:
            tokens = int(count)
            estimated = False
        metadata = getattr(response, 'metadata', None)
        billable = getattr(metadata, 'billable_character_count', None)
        if isinstance(billable, int) and billable >= 0:
            characters = billable
        status = 'ok'
        return vector
    except EmbeddingError:
        raise
    except Exception as error:
        logger.warning('Vertex embedding failed: %s', type(error).__name__)
        raise EmbeddingError('Смысловой поиск пока недоступен. Поиск по словам продолжает работать.') from error
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                logger.warning('Vertex embedding close failed: %s', type(error).__name__)
        _record_usage(provider='vertex', model=MODEL, kind='embedding',
                      input_tokens=tokens, output_tokens=0, thinking_tokens=0,
                      billable_characters=characters, estimated=estimated, status=status)
