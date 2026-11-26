import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import date,datetime,timezone,timedelta
from pathlib import Path

from .common import DomainError, digest, normalize_api_key


@dataclass
class Settings:
    def target_covered(self, target):
        """Temporary source coverage policy, independent from POI search capability."""
        if target.get('target_poi') == self.target_poi:
            return True
        name = re.sub(r'[\s（）()]', '', str(target.get('target_name', '')))
        return target.get('city') == '深圳市' and bool(re.fullmatch(
            r'香港中文大学深圳(?:上园|下园|南门|北门|东门|西门|校本部)?', name))

    database: str = field(default_factory=lambda: str(Path(__file__).resolve().parents[1] / '.runtime/business.sqlite'))
    host: str = '127.0.0.1'
    port: int = 18080
    target_poi: str = 'B0FFF4H4MQ'
    target_location: str = '114.212820,22.688319'
    target_address: str = '深圳市龙岗区龙翔大道2001号'
    reference_date: str | None = None
    calendar_policy: str = 'workday'
    planning_context_verified: bool = False
    amap_key: str = ''
    model_endpoint: str = 'https://openrouter.ai/api/v1/chat/completions'
    model_name: str = 'z-ai/glm-5.3-flash'
    model_key: str = ''
    model_protocol: str = 'chat_completions'
    public_origin: str = ''
    ga_measurement_id: str = ''
    model_timeout: int = 90
    model_max_tokens: int = 4096
    model_reasoning_effort: str = ''
    model_input_limit: int = 100000
    model_call_limit: int = 100
    map_call_limit: int = 3000
    station_catalog: str = ''
    station_table: str = field(default_factory=lambda: str(Path(__file__).resolve().parents[3] / 'data' / 'station-table.json'))
    cookie_secure: bool = False
    session_days: int = 365
    receipt_days: int = 7
    requests_per_minute: int = 120
    task_limit_per_day: int = 30
    quota_near_target: int = 5
    max_attempts_near_target: int = 10
    quota_near_metro: int = 5
    max_attempts_near_metro: int = 10

    @classmethod
    def from_env(cls):
        settings = cls()
        mapping = {'database': 'RENTAL_DATABASE', 'reference_date': 'COMMUTE_REFERENCE_DATE',
                   'amap_key': 'AMAP_WEB_KEY', 'model_endpoint': 'MODEL_ENDPOINT',
                   'model_name': 'MODEL_NAME', 'model_key': 'MODEL_API_KEY',
                   'public_origin': 'PUBLIC_ORIGIN', 'ga_measurement_id': 'GA_MEASUREMENT_ID',
                   'model_reasoning_effort': 'MODEL_REASONING_EFFORT',
                   'station_catalog': 'STATION_CATALOG_PATH', 'station_table': 'STATION_TABLE_PATH'}
        for field_name, env_name in mapping.items():
            if env_name in os.environ:
                setattr(settings, field_name, os.environ[env_name])
        settings.planning_context_verified = os.environ.get('AMAP_PLANNING_CONTEXT_VERIFIED') == '1'
        if settings.reference_date == 'today':
            settings.reference_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            settings.calendar_policy = 'actual_date'
        settings.model_key = normalize_api_key(settings.model_key)
        settings.cookie_secure = os.environ.get('COOKIE_SECURE') == '1'
        for field_name in ('port', 'session_days', 'receipt_days', 'requests_per_minute',
                           'task_limit_per_day', 'model_input_limit', 'model_call_limit', 'model_max_tokens', 'model_timeout', 'map_call_limit',
                           'quota_near_target', 'max_attempts_near_target', 'quota_near_metro', 'max_attempts_near_metro'):
            value = int(os.environ.get('RENTAL_' + field_name.upper(), getattr(settings, field_name)))
            if value <= 0:
                raise ValueError(field_name + ' must be positive')
            setattr(settings, field_name, value)
        settings.validate_deployment()
        return settings

    def validate_deployment(self):
        if self.ga_measurement_id and not re.fullmatch(r'G-[A-Z0-9]+', self.ga_measurement_id):
            raise ValueError('GA_MEASUREMENT_ID必须是G-开头的衡量ID')
        if self.public_origin:
            origin = urlsplit(self.public_origin)
            if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                    or origin.path or origin.query or origin.fragment):
                raise ValueError('PUBLIC_ORIGIN必须是无路径的HTTPS网站地址，例如https://rent.example.com')
            if not self.cookie_secure:
                raise ValueError('公开HTTPS部署必须设置COOKIE_SECURE=1')
        if self.model_endpoint:
            endpoint = urlsplit(self.model_endpoint)
            if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
                    or endpoint.query or endpoint.fragment):
                raise ValueError('MODEL_ENDPOINT必须是无凭据、无查询参数的完整HTTPS推理地址')

    def fixed_config(self):
        config = {'city': '深圳市', 'target_name': '香港中文大学（深圳）',
                  'target_address': self.target_address, 'target_poi': self.target_poi,
                  'location': self.target_location, 'commute_departure_time': '08:30',
                  'commute_timezone': 'Asia/Shanghai', 'commute_reference_date': self.reference_date,
                  'route_policy_version': 'v1.37'}
        config['config_hash'] = digest(config)
        return config

    def search_defaults(self):
        return {name: getattr(self, name) for name in ('quota_near_target', 'max_attempts_near_target',
                                                      'quota_near_metro', 'max_attempts_near_metro')}

    def planning_context(self):
        try:
            reference = date.fromisoformat(self.reference_date or '')
            if reference.weekday() > 4 and self.calendar_policy != 'actual_date':
                raise ValueError()
        except ValueError:
            raise DomainError('PLANNING_DATE_REQUIRED', '请配置已核对且可规划的代表工作日。', 503)
        context = {'reference_date': reference.isoformat(), 'departure_time': '08:30', 'timezone': 'Asia/Shanghai',
                'direction': 'to_target', 'route_policy_version': 'v1.37',
                'planning_context_verified': self.planning_context_verified}
        if self.calendar_policy == 'actual_date':context['calendar_policy'] = 'actual_date'
        return context
