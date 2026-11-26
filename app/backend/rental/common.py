import hashlib
import json
import uuid
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + '_' + uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def decode(value, default=None):
    return json.loads(value) if value is not None else default


class DomainError(Exception):
    def __init__(self, code, message, status=409, retryable=False, **details):
        self.code, self.message, self.status = code, message, status
        self.retryable, self.details = retryable, details
        super().__init__(message)


class StaleTask(Exception):
    pass
def normalize_api_key(value):
    """Remove copy/paste wrappers only; never alter the interior of a credential."""
    value = value.strip().strip('\ufeff\u200b').strip()
    for _ in range(4):
        before = value
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].strip()
        if value.lower().startswith('authorization:'):
            value = value[len('authorization:'):].strip()
        if value.lower().startswith('bearer '):
            value = value[7:].strip()
        if value == before:
            break
    return value

