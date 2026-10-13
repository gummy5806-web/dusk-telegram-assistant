"""The personal embedding boundary uses Vertex only and never logs text."""
import importlib
import importlib.util
import types
import unittest
from unittest.mock import Mock, patch

class EmbeddingBoundaryTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('llm_embeddings'), 'Embedding boundary is not implemented')
        return importlib.import_module('llm_embeddings')

    def sdk(self, vector):
        client=Mock()
        client.models.embed_content.return_value=types.SimpleNamespace(embeddings=[types.SimpleNamespace(values=vector,statistics=types.SimpleNamespace(token_count=8))],metadata=types.SimpleNamespace(billable_character_count=24))
        genai=types.ModuleType('google.genai'); genai.Client=Mock(return_value=client)
        sdk_types=types.ModuleType('google.genai.types'); sdk_types.EmbedContentConfig=lambda **kw:kw
        google=types.ModuleType('google'); google.genai=genai
        return client,genai,{'google':google,'google.genai':genai,'google.genai.types':sdk_types}

    def test_valid_query_uses_vertex_retrieval_query_and_accounts(self):
        mod=self.module(); client,sdk,modules=self.sdk([1.0,0.0])
        with patch.dict('sys.modules',modules),patch('llm._project',return_value='test-project'),patch.object(mod,'_record_usage') as record:
            value=mod.embed('инструмент для документов','RETRIEVAL_QUERY')
        self.assertEqual(value,[1.0,0.0])
        self.assertTrue(sdk.Client.call_args.kwargs['vertexai'])
        self.assertEqual(client.models.embed_content.call_args.kwargs['model'],'gemini-embedding-001')
        self.assertEqual(client.models.embed_content.call_args.kwargs['config']['task_type'],'RETRIEVAL_QUERY')
        self.assertEqual(record.call_args.kwargs['status'],'ok')
        client.close.assert_called_once()

    def test_nonfinite_vector_is_rejected_without_text_in_error(self):
        mod=self.module(); client,sdk,modules=self.sdk([float('nan'),0.0])
        with patch.dict('sys.modules',modules),patch('llm._project',return_value='test-project'),patch.object(mod,'_record_usage') as record:
            with self.assertRaises(mod.EmbeddingError) as error:mod.embed('private text','RETRIEVAL_DOCUMENT')
        self.assertNotIn('private text',str(error.exception))
        self.assertEqual(record.call_args.kwargs['status'],'error')
        client.close.assert_called_once()

    def test_empty_or_unknown_task_never_calls_provider(self):
        mod=self.module()
        for text,kind in [('', 'RETRIEVAL_QUERY'),('word','CLASSIFICATION'),('x'*5000,'RETRIEVAL_DOCUMENT')]:
            with self.assertRaises(mod.EmbeddingError):mod.embed(text,kind)
