import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import inspect
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import ValidationError, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import urlsplit

from . import schemas
from .common import DomainError, identifier
from .config import Settings
from .db import Database
from .engine import Engine
from .providers import ModelGateway, MapGateway
from .service import Services
from .assistants import Assistants
from .assistant_schemas import AssistantCommand, Upload
from .documents import decode_upload


COOKIE = 'rental_session'
WEB = Path(__file__).resolve().parents[1] / 'web'


class ProcessLock:
    def __init__(self, database):
        self.database, self.file = database, None

    def acquire(self):
        if self.database == ':memory:':
            return
        import os
        target = Path(self.database + '.worker.lock')
        target.parent.mkdir(parents=True, exist_ok=True)
        self.file = target.open('a+b')
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not target.stat().st_size:
                self.file.write(b'1')
                self.file.flush()
                self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError('同一数据库已有执行器运行，禁止双实例重复收费。') from None
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


def create_app(settings=None, *, model=None, maps=None, database=None, run_worker=True):
    settings = settings or Settings.from_env()
    settings.validate_deployment()
    database = database or Database(settings.database)
    model = model or ModelGateway(settings)
    maps = maps or MapGateway(settings)
    services = Services(database, settings, getattr(model, 'prompt_version', ModelGateway.prompt_version))
    services.station_selector=lambda target,limit,context:maps.station_plan(target,limit,context)
    if isinstance(maps, MapGateway): maps.station_tables = services.station_tables
    engine = Engine(services, model, maps)
    assistants = Assistants(services, model)
    process_lock = ProcessLock(settings.database)
    secret_path = Path(settings.database).parent / 'server.secret' if settings.database != ':memory:' else None
    if secret_path:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with secret_path.open('xb') as handle:
                handle.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        secret = secret_path.read_bytes()
    else:
        secret = secrets.token_bytes(32)

    def csrf(token):
        return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app):
        process_lock.acquire()
        if run_worker:
            engine.start()
            assistants.start()
        try:
            yield
        finally:
            engine.stop()
            assistants.stop()
            process_lock.release()
            # An active provider may still be returning; don't close its connection underneath it.

    specs = []
    rate_limits = defaultdict(deque)

    def route(method, path, model_class, action, *, initialize=False):
        async def endpoint(request: Request):
            request_id = identifier('req')
            new_cookie = None
            try:
                token = request.cookies.get(COOKIE)
                owner = hashlib.sha256(token.encode()).hexdigest() if token else None
                ticks = rate_limits[owner or request.client.host if request.client else 'anonymous']
                clock = time.monotonic()
                while ticks and ticks[0] < clock - 60:
                    ticks.popleft()
                if len(ticks) >= settings.requests_per_minute:
                    raise DomainError('RATE_LIMITED', '请求过于频繁，请稍后重试。', 429, True, retry_after_sec=60)
                ticks.append(clock)
                if method != 'GET':
                    origin = request.headers.get('origin')
                    expected_origin = settings.public_origin or str(request.base_url).rstrip('/')
                    if origin != expected_origin:
                        raise DomainError('CSRF_FAILED', '写请求来源不合法。', 403)
                    if token and owner and database.one('SELECT profile_id FROM profiles WHERE owner_hash=?', (owner,)):
                        try:
                            services.profile(owner)
                            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(token)):
                                raise DomainError('CSRF_FAILED', '缺少有效CSRF凭据。', 403)
                        except DomainError as error:
                            if not initialize or error.code != 'SESSION_EXPIRED':
                                raise
                            token = secrets.token_urlsafe(32)
                            owner = hashlib.sha256(token.encode()).hexdigest()
                            new_cookie = token
                    elif initialize:
                        token = secrets.token_urlsafe(32)
                        owner = hashlib.sha256(token.encode()).hexdigest()
                        new_cookie = token
                    else:
                        raise DomainError('SESSION_REQUIRED', '请先初始化工作区。', 401)
                    raw = await request.body()
                    if len(raw) > (15000000 if request.url.path.endswith('/assistants/contract/upload') else 256000):
                        raise DomainError('INVALID_REQUEST', '请求体过大。', 413)
                    try:
                        parsed = json.loads(raw) if raw else {}
                        data = model_class.model_validate(parsed)
                    except (ValueError, ValidationError) as error:
                        details = error.errors(include_input=False, include_url=False) if isinstance(error, ValidationError) else []
                        # Pydantic context may include Exception instances; keep only safe field/message pairs.
                        raise DomainError('VALIDATION_FAILED', '请求字段不符合接口契约。', 422,
                                          fields=[{'field': '.'.join(map(str, item['loc'])), 'message': item['msg']} for item in details]) from None
                    operation = method + ' ' + request.url.path
                    result = services.mutate(owner, operation, request.headers.get('idempotency-key'), data.model_dump(),
                                             lambda connection: action(request, owner, data, connection))
                    engine.wake()
                else:
                    data = None
                    result = action(request, owner, None, None)
                    if inspect.isawaitable(result):
                        result = await result
                if path in ('/api/v1/workspace', '/api/v1/profile/reset') and token:
                    result = {**result, 'csrf_token': csrf(token)}
                result = TypeAdapter(schemas.output_type(method, path)).validate_python(result)
                result = TypeAdapter(schemas.output_type(method, path)).dump_python(result, mode='json')
                code = 202 if isinstance(result, dict) and result.get('task_id') and method != 'GET' else 201 if new_cookie else 200
                response = JSONResponse({'request_id': request_id, 'data': result}, status_code=code)
                if new_cookie:
                    response.set_cookie(COOKIE, new_cookie, httponly=True, secure=settings.cookie_secure, samesite='strict', max_age=settings.session_days * 86400)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except DomainError as error:
                return JSONResponse({'request_id': request_id, 'error': {'code': error.code, 'message': error.message,
                                    'retryable': error.retryable, 'details': error.details}}, status_code=error.status,
                                    headers={'Cache-Control': 'no-store'})
            except (sqlite3.Error, ValueError, KeyError):
                return JSONResponse({'request_id': request_id, 'error': {'code': 'INTERNAL_ERROR', 'message': '操作未完成，事务已回滚。',
                                    'retryable': True, 'details': {}}}, status_code=503)
        specs.append({'method': method, 'path': path, 'model': model_class, 'endpoint': endpoint})

    route('GET', '/api/v1/workspace', None, lambda r, o, d, c: services.workspace(o))
    route('POST', '/api/v1/workspace', schemas.Empty, lambda r, o, d, c: services.initialize(c, o), initialize=True)
    async def target_candidates(request, owner, data, connection):
        profile = services.profile(owner)
        if profile['state'] != 'draft':
            raise DomainError('INVALID_STATE', '画像已锁定，不能更换目的地。')
        query = request.query_params.get('q', '').strip()
        if not 2 <= len(query) <= 100:
            raise DomainError('INVALID_QUERY', '目的地请输入2至100个字符。', 422)
        if not hasattr(maps, 'search_targets'):
            raise DomainError('PROVIDER_NOT_CONFIGURED', '地址查询接口尚未配置。', 503)
        places = await asyncio.to_thread(maps.search_targets, query)
        items = []
        for place in places[:10]:
            payload = {'profile_id': profile['profile_id'], 'expires': int(time.time()) + 900,
                       'config_hash': settings.fixed_config()['config_hash'], 'place': place}
            encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
            signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
            items.append({**place, 'candidate_token': encoded + '.' + signature,
                          'data_covered': settings.target_covered(place)})
        return {'items': items, 'query': query}

    def select_target(request, owner, data, connection):
        selected = None
        if data.candidate_token:
            try:
                encoded, signature = data.candidate_token.rsplit('.', 1)
                expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError('signature')
                payload = json.loads(base64.urlsafe_b64decode(encoded))
                if payload['profile_id'] != data.profile_id or payload['expires'] < time.time() or payload['config_hash'] != settings.fixed_config()['config_hash']:
                    raise ValueError('expired or different profile')
                selected = payload['place']
            except (ValueError, KeyError, TypeError):
                raise DomainError('TARGET_SELECTION_EXPIRED', '地址候选已失效，请重新查询并选择。', 409) from None
        return services.confirm_target(connection, owner, data, selected)

    route('GET', '/api/v1/capabilities', None, lambda r, o, d, c: maps.capabilities() if hasattr(maps, 'capabilities') else {'station_discovery': {'status': 'unavailable'}})
    route('GET', '/capabilities', None, lambda r, o, d, c: maps.capabilities() if hasattr(maps, 'capabilities') else {'station_discovery': {'status': 'unavailable'}})
    route('GET', '/api/v1/target-candidates', None, target_candidates)
    route('POST', '/api/v1/profile/confirm-target', schemas.ConfirmTarget, select_target)
    route('POST', '/api/v1/profile/save', schemas.SaveProfile, lambda r, o, d, c: services.save_profile(c, o, d))
    route('POST', '/api/v1/profile/reset', schemas.ResetProfile, lambda r, o, d, c: services.reset(c, o, d))
    route('GET', '/api/v1/search-preflight', None, lambda r,o,d,c:services.search_preflight(o))
    route('POST', '/api/v1/tasks/search', schemas.SearchCommand, lambda r, o, d, c: services.submit_search(c, o, d, r.headers['idempotency-key']))
    route('POST', '/api/v1/tasks/import', schemas.ImportTask, lambda r, o, d, c: services.submit_import(c, o, d, r.headers['idempotency-key']))
    route('GET', '/api/v1/tasks/{id}', None, lambda r, o, d, c: services.task(o, r.path_params['id']))
    route('POST', '/api/v1/tasks/{id}/retry', schemas.Command, lambda r, o, d, c: services.retry(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/tasks/{id}/skip', schemas.Command, lambda r, o, d, c: services.skip_current(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/tasks/{id}/location', schemas.ResolveLocation, lambda r, o, d, c: services.resolve_location(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/tasks/{id}/cancel', schemas.CancelTask, lambda r, o, d, c: services.cancel(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/records/{id}/resume', schemas.Command, lambda r, o, d, c: services.resume(c, o, r.path_params['id'], d, r.headers['idempotency-key']))
    route('POST', '/api/v1/conditions/{id}/decision', schemas.Decision, lambda r, o, d, c: services.decide(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/records/{id}/community', schemas.Supplement, lambda r, o, d, c: services.supplement(c, o, r.path_params['id'], d))

    def limit(request):
        try:
            return int(request.query_params.get('limit', '20'))
        except ValueError:
            raise DomainError('INVALID_REQUEST', '分页数量必须是整数。', 422) from None

    route('GET', '/api/v1/records', None, lambda r, o, d, c: services.page(o, 'candidate', r.query_params.get('cursor'), limit(r)))
    route('GET', '/api/v1/history', None, lambda r, o, d, c: {
        'archived': services.page(o, 'archived', r.query_params.get('archived_cursor'), limit(r)),
        'screened': services.page(o, 'screened', r.query_params.get('screened_cursor'), limit(r))})
    route('GET', '/api/v1/records/{id}', None, lambda r, o, d, c: services.record(o, r.path_params['id']))
    route('PATCH', '/api/v1/records/{id}', schemas.PatchRecord, lambda r, o, d, c: services.patch(c, o, r.path_params['id'], d))
    route('POST', '/api/v1/records/archive', schemas.Archive, lambda r, o, d, c: services.archive(c, o, d))
    route('POST', '/api/v1/records/archive-all', schemas.ArchiveAll, lambda r, o, d, c: services.archive_all(c, o, d))
    route('POST', '/api/v1/records/{id}/restore', schemas.Command, lambda r, o, d, c: services.restore(c, o, r.path_params['id'], d))
    route('GET', '/api/v1/reports/{id}', None, lambda r, o, d, c: services.report(o, r.path_params['id']))
    route('GET', '/api/v1/evidence/{id}', None, lambda r, o, d, c: services.evidence(o, r.path_params['id']))
    route('GET', '/api/v1/records/{id}/viewing', None, lambda r, o, d, c: services.viewing(o, r.path_params['id']))
    route('PUT', '/api/v1/records/{id}/viewing', schemas.SaveViewing, lambda r, o, d, c: services.save_viewing(c, o, r.path_params['id'], d))

    route('GET', '/api/v1/records/{id}/assistants/{kind}', None,
          lambda r,o,d,c: assistants.state(o,r.path_params['id'],r.path_params['kind']))
    route('POST', '/api/v1/records/{id}/assistants/{kind}', AssistantCommand,
          lambda r,o,d,c: assistants.command(c,o,r.path_params['id'],r.path_params['kind'],d))
    route('POST', '/api/v1/records/{id}/assistants/contract/upload', Upload,
          lambda r,o,d,c: assistants.upload(c,o,r.path_params['id'],d,*decode_upload(d)))
    route('GET', '/api/v1/records/{id}/assistants/contract/files/{file}', None,
          lambda r,o,d,c: assistants.file(o,r.path_params['id'],r.path_params['file']))

    def openapi_document():
        paths, components = {}, {}
        for spec in specs:
            schema_class = spec['model']
            operation = {'operationId': spec['method'].lower() + spec['path'].replace('/', '_').replace('{', '').replace('}', ''),
                         'responses': {'200': {'description': '成功，通用request_id/data响应'},
                                       '202': {'description': '异步任务已接收'}, '422': {'description': '字段校验失败'},
                                       '409': {'description': '版本、幂等或状态冲突'}, '503': {'description': '明确技术失败'}}}
            parameters = [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}] if '{id}' in spec['path'] else []
            if schema_class:
                schema = schema_class.model_json_schema(ref_template='#/components/schemas/{model}')
                components.update(schema.pop('$defs', {}))
                components[schema_class.__name__] = schema
                operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/' + schema_class.__name__}}}}
                parameters += [{'name': 'Idempotency-Key', 'in': 'header', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'X-CSRF-Token', 'in': 'header', 'required': spec['path'] != '/api/v1/workspace', 'schema': {'type': 'string'}}]
            output = TypeAdapter(schemas.output_type(spec['method'], spec['path'])).json_schema(ref_template='#/components/schemas/{model}')
            components.update(output.pop('$defs', {}))
            envelope = {'type': 'object', 'required': ['request_id', 'data'], 'properties': {'request_id': {'type': 'string'}, 'data': output}}
            for code in ('200', '202'):
                operation['responses'][code]['content'] = {'application/json': {'schema': envelope}}
            if spec['path'] == '/api/v1/workspace':
                operation['security'] = []
                if spec['method'] == 'POST':
                    operation['responses']['201'] = {'description': '首次匿名工作区已创建', 'content': {'application/json': {'schema': envelope}}}
            error_schema = schemas.ErrorEnvelope.model_json_schema(ref_template='#/components/schemas/{model}')
            components.update(error_schema.pop('$defs', {}))
            components['ErrorEnvelope'] = error_schema
            for code in ('400', '401', '403', '404', '409', '413', '422', '429', '503'):
                operation['responses'][code] = {'description': '通用错误；不包含请求凭据', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}}
            operation['parameters'] = parameters
            paths.setdefault(spec['path'], {})[spec['method'].lower()] = operation
        return {'openapi': '3.1.0', 'info': {'title': '租房助手业务API', 'version': '1.37.0'},
                'paths': paths, 'security': [{'anonymousSession': []}],
                'components': {'schemas': components, 'securitySchemes': {'anonymousSession': {'type': 'apiKey', 'in': 'cookie', 'name': COOKIE}}}}

    async def openapi(request):
        return JSONResponse(openapi_document())

    async def health(request):
        return JSONResponse({'alive': True})

    async def ready(request):
        reasons = []
        if not database.one('SELECT import_id FROM imports LIMIT 1'):
            reasons.append('SOURCE_REQUIRED')
        if not model.ready():
            reasons.append('MODEL_NOT_CONFIGURED')
        if not maps.ready():
            reasons.append('MAP_NOT_READY')
        return JSONResponse({'ready': not reasons, 'reasons': reasons}, status_code=503 if reasons else 200)

    async def index(request):
        analytics_sources = " https://www.googletagmanager.com" if settings.ga_measurement_id else ''
        analytics_connect = " https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com" if settings.ga_measurement_id else ''
        return FileResponse(WEB / 'index.html', headers={'Cache-Control': 'no-store',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Content-Security-Policy': f"default-src 'self'; script-src 'self'{analytics_sources}; style-src 'self'; img-src 'self' data:{analytics_connect}; connect-src 'self'{analytics_connect}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})

    async def public_config(request):
        return JSONResponse({'ga_measurement_id': settings.ga_measurement_id}, headers={'Cache-Control': 'no-store'})

    routes = [Route('/healthz', health), Route('/readyz', ready), Route('/openapi.json', openapi), Route('/public-config.json', public_config), Route('/', index)]
    routes += [Route(item['path'], item['endpoint'], methods=[item['method']]) for item in specs]
    if WEB.exists():
        routes.append(Mount('/assets', StaticFiles(directory=WEB), name='assets'))
    app = Starlette(routes=routes, lifespan=lifespan)
    if settings.public_origin:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.public_origin).hostname])
    app.state.database, app.state.services, app.state.engine = database, services, engine
    app.state.settings = settings
    app.state.assistants = assistants
    return app
