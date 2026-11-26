import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
import urllib.error

from rental.common import DomainError
from rental.config import Settings
from rental.providers import ModelGateway, MapGateway


class ProviderFailureBranches(TestCase):
    def settings(self, **kwargs):
        return Settings(model_endpoint='https://provider.invalid/chat/completions', model_name='test', model_key='test-secret',
                        reference_date='2026-11-17', planning_context_verified=True, **kwargs)

    def test_http_auth_billing_permission_rate_and_server_errors(self):
        """鉴权、欠费、权限、限流、服务故障区分可重试性且不泄漏凭据。"""
        for status in (400, 401, 402, 403, 408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://provider.invalid', status, '', {}, io.BytesIO(b'private response'))
                with patch('urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(DomainError) as raised:
                        ModelGateway(self.settings()).invoke('classification', {})
                self.assertEqual(raised.exception.code, 'MODEL_HTTP_'+str(status))
                self.assertEqual(raised.exception.retryable, status in (408, 429, 500, 502, 503, 504))
                self.assertNotIn('test-secret', str(raised.exception))

    def test_timeout_and_unreachable_provider_are_explicit(self):
        """超时和网络不可达不生成模型成功结果。"""
        for error in (TimeoutError(), urllib.error.URLError('private URL')):
            with self.subTest(error=type(error).__name__):
                with patch('urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(DomainError) as raised:
                        ModelGateway(self.settings()).invoke('classification', {})
                self.assertEqual(raised.exception.code, 'MODEL_NETWORK_ERROR')
                self.assertTrue(raised.exception.retryable)

    def test_null_non_json_and_wrong_schema_outputs_are_rejected(self):
        """空内容、非JSON、字段类型错误和多余字段不会写成功。"""
        contents = [None, 'not JSON', '{"type":"rental","reason":7}',
                    '{"type":"rental","reason":"x","extra":true}', '[]']
        for content in contents:
            with self.subTest(content=content):
                response = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
                with patch('urllib.request.urlopen', return_value=io.BytesIO(response)):
                    with self.assertRaises(DomainError) as raised:
                        ModelGateway(self.settings()).invoke('classification', {})
                self.assertEqual(raised.exception.code, 'AI_INVALID_OUTPUT')

    def test_input_limit_and_insecure_endpoint_fail_before_network(self):
        """超长输入不截断证据，非HTTPS配置不发请求。"""
        for kind in ('length', 'endpoint'):
            settings = self.settings()
            if kind == 'length': settings.model_input_limit = 1
            else: settings.model_endpoint = 'http://provider.invalid/chat/completions'
            with self.subTest(kind=kind):
                with patch('urllib.request.urlopen', side_effect=AssertionError('must not request')):
                    with self.assertRaises(DomainError) as raised:
                        ModelGateway(settings).invoke('classification', {'body': 'source'})
                self.assertEqual(raised.exception.code, 'MODEL_INPUT_LIMIT' if kind == 'length' else 'PROVIDER_CONFIGURATION_ERROR')

    def test_map_ambiguous_location_is_not_silently_first_candidate(self):
        """重名定位不取第一项伪造可靠通勤。"""
        settings = self.settings(amap_key='test-map-key')
        gateway = MapGateway(settings)
        client = Mock()
        client.resolve_place.return_value = {'candidates': [
            {'name': '测试小区', 'cityname': '深圳市', 'location': '1,2'},
            {'name': '测试小区', 'cityname': '深圳市', 'location': '3,4'}]}
        with patch.object(gateway, '_client', return_value=client):
            with self.assertRaises(DomainError) as raised:
                gateway.evaluate('测试小区', [], settings.fixed_config(), 2400, 600, settings.planning_context())
        self.assertEqual(raised.exception.code, 'LOCATION_UNCONFIRMED')
        client.evaluate.assert_not_called()

    def test_map_missing_route_durations_are_not_business_failure(self):
        """高德缺可靠路线是技术未确认，不写符合或业务不符。"""
        settings = self.settings(amap_key='test-map-key')
        gateway = MapGateway(settings)
        client = Mock()
        client.evaluate.return_value = {'quality': 'unconfirmed'}
        refs = [{'name': '测试小区', 'location': '114.2,22.6', 'verified': True, 'source_ref': 'verified-source'}]
        with patch.object(gateway, '_client', return_value=client):
            with self.assertRaises(DomainError) as raised:
                gateway.evaluate('测试小区', refs, settings.fixed_config(), 2400, 600, settings.planning_context())
        self.assertEqual(raised.exception.code, 'ROUTE_UNCONFIRMED')

    def test_station_legacy_policy_cannot_silently_switch(self):
        settings = self.settings(amap_key='test-map-key')
        with self.assertRaises(DomainError) as raised:
            MapGateway(settings).station_plan(settings.fixed_config(), 2400, settings.planning_context())
        self.assertEqual(raised.exception.code, 'LEGACY_DISCOVERY_POLICY')
