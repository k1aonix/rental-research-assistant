"""Read-only OpenRouter credential preflight. Never emits credentials, account IDs or balances."""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from rental.common import normalize_api_key


def check(endpoint, key):
    host = urlsplit(endpoint).hostname
    if host != 'openrouter.ai':
        return {'ok': True, 'state': 'not_applicable'}
    key = normalize_api_key(key)
    if not key:
        return {'ok': False, 'state': 'key_empty'}
    if any(char.isspace() for char in key):
        return {'ok': False, 'state': 'key_contains_internal_whitespace'}
    if not key.isascii():
        return {'ok': False, 'state': 'key_contains_non_ascii'}
    if key.lower().startswith(('https://', 'http://')):
        return {'ok': False, 'state': 'key_is_url'}
    request = urllib.request.Request('https://openrouter.ai/api/v1/key',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)['data']
        if data.get('is_management_key') or data.get('is_provisioning_key'):
            return {'ok': False, 'state': 'management_key_not_inference_key'}
        remaining = data.get('limit_remaining')
        if type(remaining) in (int, float) and remaining <= 0:
            return {'ok': False, 'state': 'key_limit_exhausted'}
        return {'ok': True, 'state': 'authenticated'}
    except urllib.error.HTTPError as error:
        # Match known error wording, never expose arbitrary upstream response text.
        detail = error.read(4096).decode('utf-8', errors='replace').lower()
        category = 'rejected'
        for marker, name in (('user not found', 'user_not_found'), ('no auth credentials', 'no_auth_credentials'),
                             ('no cookie auth credentials', 'no_auth_credentials'), ('expired', 'expired'),
                             ('disabled', 'disabled'), ('revoked', 'revoked')):
            if marker in detail:
                category = name
                break
        return {'ok': False, 'state': 'http_error', 'http_status': error.code, 'category': category}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ok': False, 'state': 'network_or_invalid_response'}


def main():
    result = check(os.environ.get('MODEL_ENDPOINT', ''), os.environ.get('MODEL_API_KEY', '').strip())
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['ok'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
