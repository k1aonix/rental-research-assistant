import unittest
from starlette.testclient import TestClient
from rental.api import create_app
from rental.config import Settings


class DeploymentTest(unittest.TestCase):
    def test_public_config_never_exposes_provider_credentials(self):
        settings = Settings(database=':memory:', public_origin='https://rent.example.com', cookie_secure=True,
                            ga_measurement_id='G-ABC123', model_key='not-for-browser', amap_key='not-for-browser-either')
        with TestClient(create_app(settings, run_worker=False), base_url='https://rent.example.com') as client:
            self.assertEqual(client.get('/public-config.json').json(), {'ga_measurement_id': 'G-ABC123'})
            response = client.post('/api/v1/workspace', json={}, headers={'Origin': settings.public_origin, 'Idempotency-Key': 'test-deploy-123'})
            self.assertEqual(response.status_code, 201)
            self.assertIn('Secure', response.headers['set-cookie'])
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            self.assertEqual(client.post('/api/v1/workspace', json={}, headers={'Origin': 'https://evil.example'}).status_code, 403)
            self.assertEqual(client.get('/healthz', headers={'Host': 'evil.example'}).status_code, 400)
            policy = client.get('/').headers['content-security-policy']
            self.assertIn('https://www.googletagmanager.com', policy)
            self.assertNotIn("'unsafe-inline'", policy)

    def test_analytics_disabled_and_origin_behind_proxy(self):
        settings = Settings(database=':memory:', public_origin='https://rent.example.com', cookie_secure=True)
        with TestClient(create_app(settings, run_worker=False), base_url='http://rent.example.com') as client:
            # Internal HTTP to the reverse proxy is independent of the browser's public HTTPS origin.
            response = client.post('/api/v1/workspace', json={}, headers={'Origin': settings.public_origin, 'Idempotency-Key': 'test-deploy-proxy'})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(client.get('/public-config.json').json(), {'ga_measurement_id': ''})
            self.assertNotIn('google', client.get('/').headers['content-security-policy'])

    def test_bad_deployment_configuration_fails_early(self):
        for fields in [dict(public_origin='https://rent.example.com'), dict(public_origin='http://rent.example.com', cookie_secure=True),
                       dict(public_origin='https://rent.example.com/path', cookie_secure=True), dict(ga_measurement_id='G-X;evil'),
                       dict(model_endpoint='http://provider.example/chat/completions'), dict(model_endpoint='https://provider.example/chat?key=secret')]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Settings(**fields).validate_deployment()

    def test_openrouter_defaults_and_timeout_environment(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'RENTAL_MODEL_TIMEOUT': '120'}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.model_timeout, 120)
        self.assertEqual(settings.model_endpoint, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(settings.model_name, 'z-ai/glm-5.3-flash')

    def test_today_uses_beijing_date_and_accepts_actual_weekend(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        from amap_backend import AmapClient
        with patch.dict('os.environ', {'COMMUTE_REFERENCE_DATE':'today'},clear=True):
            with patch('rental.config.datetime') as clock:
                clock.now.return_value=datetime(2026,11,21,9,0,tzinfo=timezone.utc)
                settings=Settings.from_env()
        self.assertEqual(settings.reference_date,'2026-11-21')
        context=settings.planning_context()
        self.assertEqual(context['calendar_policy'],'actual_date')
        AmapClient.validate_context(context)
        with self.assertRaises(Exception):Settings(reference_date='2026-11-21').planning_context()
