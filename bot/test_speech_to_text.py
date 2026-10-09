#!/usr/bin/env python3
"""Unit tests for the bounded Deepgram proxy client."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import speech_to_text


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]


class DeepgramProxyClientTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        handle.write(b"OggS-test-audio")
        handle.close()
        self.audio = Path(handle.name)
        self.addCleanup(self.audio.unlink, missing_ok=True)

    @patch("speech_to_text.urllib.request.urlopen")
    def test_posts_audio_to_nova3_multi_proxy(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({
            "results": {"channels": [{"alternatives": [{
                "transcript": "  мысль про оффер  "
            }]}]}
        })

        transcript = speech_to_text.transcribe_file(
            self.audio,
            proxy_url="http://127.0.0.1:8321/v1/listen",
            client_key="local-secret",
        )

        self.assertEqual(transcript, "мысль про оффер")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b"OggS-test-audio")
        self.assertIn("model=nova-3", request.full_url)
        self.assertIn("language=multi", request.full_url)
        self.assertIn("smart_format=true", request.full_url)
        self.assertEqual(request.get_header("Authorization"),
                         "Bearer local-secret")
        self.assertEqual(request.get_header("Content-type"), "audio/ogg")

    @patch.dict(os.environ, {
        "DEEPGRAM_PROXY_URL": "http://127.0.0.1:9999/custom/listen",
        "DEEPGRAM_PROXY_CLIENT_KEY": "env-secret",
        "DEEPGRAM_MODEL": "nova-3",
        "DEEPGRAM_LANGUAGE": "ru",
    }, clear=False)
    @patch("speech_to_text.urllib.request.urlopen")
    def test_reads_proxy_configuration_from_environment(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({
            "results": {"channels": [{"alternatives": [{
                "transcript": "готово"
            }]}]}
        })

        self.assertEqual(speech_to_text.transcribe_file(self.audio), "готово")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.startswith(
            "http://127.0.0.1:9999/custom/listen?"))
        self.assertIn("language=ru", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer env-secret")

    @patch("speech_to_text.urllib.request.urlopen")
    def test_rejects_malformed_deepgram_response(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({"results": {"channels": []}})
        with self.assertRaises(speech_to_text.TranscriptionError):
            speech_to_text.transcribe_file(self.audio)

    def test_rejects_oversized_audio_before_network(self) -> None:
        with patch.object(speech_to_text, "MAX_AUDIO_BYTES", 4):
            with self.assertRaises(speech_to_text.TranscriptionError):
                speech_to_text.transcribe_file(self.audio)


if __name__ == "__main__":
    unittest.main()
