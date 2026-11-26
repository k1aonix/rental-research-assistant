import io
import json
import unittest
from unittest.mock import patch

from rental.common import DomainError
from rental.config import Settings
from rental.providers import ModelGateway, MapGateway


class ProviderContractTest(unittest.TestCase):
    def test_duplicate_evidence_is_canonicalized_without_changing_verdict(self):
        from rental.engine import Engine
        output = {'result': 'unknown', 'evidence_ids': ['e1', 'e1', 'e2'],
                  'facts': [{'text': 'source', 'evidence_ids': ['e2', 'e2']}]}
        Engine._validate_evidence(output, {'e1', 'e2'})
        self.assertEqual(output['result'], 'unknown')
        self.assertEqual(output['evidence_ids'], ['e1', 'e2'])
        self.assertEqual(output['facts'][0]['evidence_ids'], ['e2'])

    def test_chat_adapter_schema_and_bearer_are_server_side(self):
        settings = Settings(model_endpoint='https://provider.invalid/chat/completions', model_name='chosen-later', model_key='test-secret')
        content = json.dumps({'choices': [{'message': {'content': json.dumps({'type': 'rental', 'reason': '测试分类'})}}], 'id': 'provider-request', 'usage': {'prompt_tokens': 10}}).encode()
        captured = []
        def response(request, timeout):
            captured.append(request)
            return io.BytesIO(content)
        with patch('urllib.request.urlopen', response):
            result = ModelGateway(settings).invoke('classification', {'title': '测试标题', 'body': '测试正文'})
        self.assertEqual(result['payload']['type'], 'rental')
        self.assertEqual(result['provider_request_id'], 'provider-request')
        request = captured[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')
        sent = json.loads(request.data)
        self.assertEqual(sent['model'], 'chosen-later')
        self.assertEqual(sent['response_format'], {'type': 'json_object'})
        self.assertNotIn('test-secret', json.dumps(result))
        self.assertNotIn('reasoning_effort', sent)
        self.assertIn('当前阶段：classification', sent['messages'][0]['content'])

    def test_optional_reasoning_and_output_budget_are_configurable(self):
        settings = Settings(model_endpoint='https://provider.invalid/chat/completions', model_name='glm-5.3-flash',
                            model_key='test-secret', model_reasoning_effort='low', model_max_tokens=8192)
        content = json.dumps({'choices': [{'message': {'content': '{"type":"unknown","reason":"unknown"}'}}]}).encode()
        captured = []
        def response(request, timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(content)
        with patch('urllib.request.urlopen', response):
            ModelGateway(settings).invoke('classification', {})
        self.assertEqual(captured[0]['reasoning_effort'], 'low')
        self.assertEqual(captured[0]['max_tokens'], 8192)

    def test_unconfigured_model_never_falls_back_to_fixtures(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('must not request')):
            with self.assertRaises(DomainError) as error:
                ModelGateway(Settings()).invoke('classification', {})
        self.assertEqual(error.exception.code, 'PROVIDER_NOT_CONFIGURED')

    def test_invalid_provider_json_does_not_become_a_report(self):
        settings = Settings(model_endpoint='https://provider.invalid/chat/completions', model_name='later', model_key='test-secret')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"choices":[]}')):
            with self.assertRaises(DomainError) as error:
                ModelGateway(settings).invoke('classification', {})
        self.assertEqual(error.exception.code, 'AI_INVALID_OUTPUT')
        self.assertNotIn('test-secret', str(error.exception))

    def test_map_readiness_requires_verified_valid_workday(self):
        settings = Settings(amap_key='test-secret', reference_date='2026-11-21', planning_context_verified=True)
        self.assertFalse(MapGateway(settings).ready())
        settings.reference_date = '2026-11-17'
        self.assertTrue(MapGateway(settings).ready())
        settings.planning_context_verified = False
        self.assertFalse(MapGateway(settings).ready())

    def test_old_unverified_map_context_is_not_accepted(self):
        with self.assertRaises(DomainError) as error:
            MapGateway._require_context({'reference_date': '2026-11-17', 'departure_time': '08:30', 'timezone': 'Asia/Shanghai', 'direction': 'to_target'})
        self.assertEqual(error.exception.code, 'UNSUPPORTED_PLANNING_CONTEXT')
