import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from .credential_check import check
from rental.common import normalize_api_key


class CredentialCheckTest(unittest.TestCase):
    def test_auth_check_is_get_without_post_or_source_data(self):
        captured = []
        def response(request, timeout):
            captured.append(request)
            return io.BytesIO(json.dumps({'data': {'label': 'private-label', 'creator_user_id': 'private-user',
                'usage': 100, 'limit_remaining': 5}}).encode())
        with patch('urllib.request.urlopen', response):
            result = check('https://openrouter.ai/api/v1/chat/completions', 'sk-or-test')
        self.assertTrue(result['ok'])
        self.assertEqual(captured[0].method, 'GET')
        self.assertIsNone(captured[0].data)
        self.assertEqual(captured[0].get_header('Authorization'), 'Bearer sk-or-test')
        self.assertNotIn('private', json.dumps(result))

    def test_error_message_is_categorized_without_echoing_arbitrary_text(self):
        error = urllib.error.HTTPError('https://openrouter.ai/api/v1/key', 401, '', {},
            io.BytesIO(b'{"error":{"message":"User not found. sk-or-sensitive"}}'))
        with patch('urllib.request.urlopen', side_effect=error):
            result = check('https://openrouter.ai/api/v1/chat/completions', 'sk-or-sensitive')
        self.assertEqual(result['category'], 'user_not_found')
        self.assertNotIn('sensitive', json.dumps(result))

    def test_internal_whitespace_never_sends_credentials(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('must not request')):
            result = check('https://openrouter.ai/api/v1/chat/completions', 'sk-or-part1 part2')
        self.assertEqual(result['state'], 'key_contains_internal_whitespace')

    def test_copy_wrappers_are_removed_identically_for_check_and_inference(self):
        from rental.config import Settings
        wrapped = '  "Authorization: Bearer sk-or-test"\r\n'
        self.assertEqual(normalize_api_key(wrapped), 'sk-or-test')
        captured = []
        def response(request, timeout):
            captured.append(request.get_header('Authorization'))
            return io.BytesIO(b'{"data":{}}')
        with patch('urllib.request.urlopen', response):
            self.assertTrue(check('https://openrouter.ai/api/v1/chat/completions', wrapped)['ok'])
        with patch.dict('os.environ', {'MODEL_API_KEY': wrapped}):
            self.assertEqual(Settings.from_env().model_key, 'sk-or-test')
        self.assertEqual(captured, ['Bearer sk-or-test'])

    def test_unfamiliar_prefix_is_validated_by_provider(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"data":{}}')):
            self.assertTrue(check('https://openrouter.ai/api/v1/chat/completions', 'future-prefix-token')['ok'])

    def test_management_key_cannot_run_inference(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"data":{"is_management_key":true}}')):
            result = check('https://openrouter.ai/api/v1/chat/completions', 'sk-or-test')
        self.assertEqual(result['state'], 'management_key_not_inference_key')
