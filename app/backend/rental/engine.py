"""Single-executor durable investigation. External calls never hold a DB transaction."""
import sqlite3
import threading
import time

from pydantic import ValidationError

from .common import DomainError, StaleTask, decode, digest, encode, identifier, now
from .providers import AmapError, PREFERENCE_POLICY
from .schemas import MODEL_SCHEMAS
from .source import valid_note


class Engine:
    def __init__(self, services, model, maps, sleep=time.sleep):
        self.svc, self.db, self.model, self.maps = services, services.db, model, maps
        self.sleep = sleep
        self._executor = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = None
        self.worker_id = identifier('executor')

    def recover(self):
        with self.db.transaction() as connection:
            for task in self.db.all("SELECT * FROM tasks WHERE state='running'"):
                progress = decode(task['progress'])
                progress.pop('executor_token', None)
                uncertain = progress.get('model_inflight') or progress.get('map_inflight') or progress.get('station_inflight')
                if uncertain:
                    progress['last_error'] = {'code': 'PROVIDER_OUTCOME_UNKNOWN',
                        'message': '服务重启时有外部请求结果尚未持久化，可能已计费。请核查后明确重试；已成功步骤保留。',
                        'retryable': True, 'details': {'call_id': uncertain.get('call_id'), 'stage': uncertain.get('stage')}}
                connection.execute('UPDATE tasks SET state=?,progress=?,version=version+1,queued_at=? WHERE task_id=?',
                                   ('failed' if uncertain else 'queued', encode(progress), now(), task['task_id']))

    def start(self):
        self.recover()
        self.thread = threading.Thread(target=self._run, name='rental-single-worker', daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def wake(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            if not self.advance():
                self._wake.wait(0.3)
                self._wake.clear()

    @staticmethod
    def event(progress, kind, **data):
        seq = progress.get('event_seq', 0) + 1
        progress['event_seq'] = seq
        progress['events'].append({'seq': seq, 'type': kind, 'step': progress.get('current_step'), 'at': now(), 'data': data})
        if len(progress['events']) > 2000:
            progress['events'] = progress['events'][-2000:]
            progress['events_dropped'] = progress.get('events_dropped', 0) + 1

    def guard(self, task_id):
        task = self.db.one('SELECT * FROM tasks WHERE task_id=?', (task_id,))
        if not task or task['state'] not in ('queued', 'running'):
            raise StaleTask()
        if decode(task['progress']).get('executor_token') != self.worker_id:
            raise StaleTask()
        profile = self.db.one('SELECT * FROM profiles WHERE profile_id=?', (task['profile_id'],))
        if not profile or profile['state'] != 'locked':
            raise StaleTask()
        return task

    def save(self, task, progress, state='queued', record_id=...):
        self.guard(task['task_id'])
        if record_id is ...:
            record_id = task['record_id']
        self.db.connection.execute('UPDATE tasks SET state=?,record_id=?,progress=?,version=version+1,updated_at=? WHERE task_id=?',
                                  (state, record_id, encode(progress), now(), task['task_id']))
        task['record_id'] = record_id

    def advance(self, task_id=None):
        if not self._executor.acquire(blocking=False):
            return False
        task = progress = None
        try:
            with self.db.transaction() as connection:
                if task_id:
                    task = self.db.one("SELECT * FROM tasks WHERE task_id=? AND state IN('queued','running')", (task_id,))
                else:
                    task = self.db.one("SELECT * FROM tasks WHERE state='queued' ORDER BY queued_at,task_id LIMIT 1")
                if not task:
                    return False
                progress = decode(task['progress'])
                progress['executor_token'] = self.worker_id
                connection.execute("UPDATE tasks SET state='running',progress=?,version=version+1,updated_at=? WHERE task_id=?", (encode(progress), now(), task['task_id']))
            self._advance(task, progress)
        except StaleTask:
            pass  # reset/cancel wins over a late provider response.
        except DomainError as error:
            if task and progress:
                self._fail(task, progress, error)
        except AmapError as error:
            if task and progress:
                self._fail(task, progress, DomainError(error.code, '高德接口或规划上下文不可用。', 503, error.retryable))
        except Exception as error:
            if task and progress:
                code = 'DATABASE_ERROR' if isinstance(error, sqlite3.Error) else 'INTERNAL_ERROR'
                self._fail(task, progress, DomainError(code, '本步骤执行失败，已保留成功进度。', 503, True,
                                                     error_type=type(error).__name__))
        finally:
            self._executor.release()
        return task is not None

    def _fail(self, task, progress, error):
        try:
            with self.db.transaction() as connection:
                self.guard(task['task_id'])
                self.event(progress, 'step_failed', code=error.code)
                progress['last_error'] = {'code': error.code, 'message': error.message, 'retryable': error.retryable, 'details': error.details}
                if error.code=='LOCATION_UNCONFIRMED' and task['record_id']:
                    progress['pending_action']={'kind':'location','record_id':task['record_id'],'prompt':error.message}
                    self.save(task,progress,'waiting_user')
                elif error.code == 'NECESSARY_SOURCE_FAILED' and task['record_id']:
                    # Source acquisition can fail before a model step is created.
                    # Persist that failure so it cannot appear as "not started".
                    step=progress['current_step']
                    existing=self.db.one('SELECT * FROM steps WHERE record_id=? AND step_name=? AND scope_key=?',(task['record_id'],step,'main'))
                    if not existing or existing['state']!='succeeded':
                        attempts=decode(existing['attempt_log']) if existing else []
                        attempts.append({'state':'failed','at':now(),'phase':'source_read','code':error.code,'message':error.message,'model_called':False})
                        connection.execute('INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id,step_name,scope_key) DO UPDATE SET state=excluded.state,attempt_log=excluded.attempt_log',
                            (existing['step_id'] if existing else identifier('step'),task['record_id'],step,'main','failed','[]',digest({'source_failure':step}),'{}',None,None,encode(attempts)))
                    report_id = self.svc.screen(connection, task['record_id'], error.code, error.message, True, error.retryable, progress['current_step'])
                    progress['result']['brief_ids'].append(report_id)
                    self._finish_record(task, progress, 'technical_failure' if task['mode']=='auto' else 'screened')
                else:
                    self.save(task, progress, 'failed')
        except StaleTask:
            pass

    def _read(self, progress, kind, **refs):
        self.event(progress, 'source_read', source=kind, **refs)

    def _ensure_record(self, task, progress, post):
        with self.db.transaction() as connection:
            self.guard(task['task_id'])
            existing = self.db.one('SELECT * FROM records WHERE profile_id=? AND post_key=?', (task['profile_id'], post['post_key']))
            record_id = existing['record_id'] if existing else identifier('record')
            if not existing:
                connection.execute('INSERT INTO records(record_id,profile_id,post_key,created_at) VALUES(?,?,?,?)',
                                   (record_id, task['profile_id'], post['post_key'], now()))
            task['record_id'] = record_id
            if record_id not in progress['result']['record_ids']:
                progress['result']['record_ids'].append(record_id)
            self.save(task, progress, record_id=record_id)
            return self.db.one('SELECT * FROM records WHERE record_id=?', (record_id,))

    def _finish_record(self, task, progress, outcome):
        if task['record_id']:
            progress['last_record_view']={'record_id':task['record_id'],'outcome':outcome,'stopped_step':progress.get('current_step'),
                'collections':{row['step_name']:decode(row['result']).get('collection_status') for row in self.db.all("SELECT step_name,result FROM steps WHERE record_id=? AND state='succeeded' AND step_name IN ('comments','author','community','preference')",(task['record_id'],))},
                'step_states':{row['step_name']:row['state'] for row in self.db.all('SELECT step_name,state FROM steps WHERE record_id=?',(task['record_id'],)) if row['step_name']!='map_http'}}
        if progress.get('current_sample'):
            progress['current_sample']['outcome'] = outcome
            progress['current_sample']['record_id'] = task['record_id']
            progress['samples'].append(progress.pop('current_sample'))
        if task['mode'] == 'auto':
            progress['current_step'] = 'classification'
            # Per-record technical failures are retained in briefs/events; the search continues.
            progress['last_error'] = None
            self.save(task, progress, record_id=None)
        else:
            progress['result']['outcome'] = outcome
            progress['pending_action'] = None
            self.event(progress, 'task_completed', outcome=outcome)
            self.save(task, progress, 'completed')

    def _is_duplicate(self, task, note_id, post_key):
        if self.db.one('SELECT record_id FROM records WHERE profile_id=? AND post_key=?', (task['profile_id'], post_key)):
            return True
        # Non-rental results have no record but their completed classifications remain in tasks.
        for row in self.db.all('SELECT progress FROM tasks WHERE profile_id=?', (task['profile_id'],)):
            progress = decode(row['progress'])
            if any(sample.get('note_id') == note_id and sample.get('outcome') in ('not_rental', 'duplicate') for sample in progress.get('samples', [])):
                return True
        return False

    def _sample(self, task, progress):
        if progress.get('current_sample'):
            return progress['current_sample']
        snapshot = progress['config_snapshot']
        while progress['round'] <= 2:
            region = 'near_target' if progress['round'] == 1 else 'near_metro'
            if region not in progress['plan']:
                stations = None
                if region == 'near_metro':
                    progress['current_step'] = 'station_plan'
                    if snapshot.get('station_selection_strategy') != 'manual_station_times_v1':
                        raise DomainError('LEGACY_DISCOVERY_POLICY', '旧任务未冻结人工站点表，请保留历史并新建搜索。', 409)
                    frozen = snapshot['station_selection']
                    plan_key = digest({'target': snapshot['target'], 'commute_limit_sec': snapshot['commute_limit_sec'],
                                       'strategy': snapshot['station_selection_strategy'], 'table_hash': frozen.get('table_hash')})
                    result = progress.get('station_selection') if progress.get('station_selection_key') == plan_key else None
                    if result is None:
                        context={**snapshot['planning_context'], 'station_selection': frozen}
                        try:
                            result = self.maps.station_plan(snapshot['target'], snapshot['commute_limit_sec'], context)
                        except DomainError as error:
                            progress['station_selection_status']={'strategy': snapshot['station_selection_strategy'], 'selection_state':'failed','error_code':error.code}
                            progress['plan']['plan_state']='second_pending'
                            raise
                    progress['station_selection'] = result
                    progress['station_selection_key'] = plan_key
                    stations = result['stations']
                    if result.get('strategy'):
                        result['snapshot_coverage']=self.svc.source.station_coverage(snapshot['import_id'], stations)
                        result['plan_revision']=digest({'table_hash':result.get('table_hash'),'stations':stations,
                                                        'import_id':snapshot['import_id'],'bindings':result['snapshot_coverage']})
                        progress['plan']['plan_state']='second_ready' if not stations or result['snapshot_coverage']['covered_count'] else 'second_pending'
                        progress['plan']['station_selection']=result
                rows = self.svc.source.search(snapshot['import_id'], region, stations, target=snapshot['target'] if region=='near_target' else None)
                progress['plan'][region] = rows
                if region=='near_target':progress['plan']['plan_state']='first_ready'
                progress['counters'][region]['hit_count'] = len(rows)
                with self.db.transaction():
                    self.save(task, progress)
            counters = progress['counters'][region]
            limits = snapshot['search']
            cursor = progress['cursors'][region]
            rows = progress['plan'][region]
            if counters['eligible_count'] >= limits['quota_' + region] or counters['attempt_count'] >= limits['max_attempts_' + region] or cursor >= len(rows):
                progress['round'] += 1
                continue
            row = rows[cursor]
            sample = {'result_key': row['result_key'], 'search_key': row['search_key'], 'position': row['position'],
                      'note_id': row['raw_note_id'], 'region': region, 'outcome': None}
            counters['attempt_count'] += 1
            progress['current_sample'] = sample
            # Persist the claimed position before classification. Only finishing the sample advances the cursor.
            progress['cursors'][region] += 1
            self.event(progress, 'sample_claimed', result_key=row['result_key'], position=row['position'])
            with self.db.transaction():
                self.save(task, progress)
            return sample
        coverage=progress.get('station_selection', {}).get('snapshot_coverage', {})
        reasons={}
        for name,counts in progress['counters'].items():
            limits=snapshot['search']
            reasons[name]=('quota_reached' if counts['eligible_count']>=limits['quota_'+name] else
                           'attempt_limit_reached' if counts['attempt_count']>=limits['max_attempts_'+name] else 'available_snapshots_exhausted')
        progress['result']['completion_reasons']=reasons
        partial_outcome=('available_snapshots_exhausted_with_coverage_missing' if reasons['near_metro']=='available_snapshots_exhausted' else 'completed_with_coverage_missing')
        progress['result']['outcome'] = (partial_outcome if coverage.get('state')=='partial'
                                         else 'completed' if progress['result']['record_ids'] else 'no_hits')
        if coverage:
            progress['result']['snapshot_coverage']=coverage
        self.event(progress, 'task_completed', outcome=progress['result']['outcome'])
        with self.db.transaction():
            self.save(task, progress, 'completed', record_id=None)
        return None

    def _classification(self, task, progress):
        if task['mode'] == 'auto':
            sample = self._sample(task, progress)
            if sample is None:
                return
            note_id = sample['note_id']
        else:
            note_id = progress['note_id']
        progress['current_step'] = 'classification'
        if not valid_note(note_id):
            if task['mode'] == 'auto':
                original = self.db.one('SELECT raw_note_id FROM search_results WHERE result_key=?', (progress['current_sample']['result_key'],))
                self.event(progress, 'source_reread', source='search_result', result_key=progress['current_sample']['result_key'])
                if original and valid_note(original['raw_note_id']):
                    note_id = original['raw_note_id']
                    progress['current_sample']['note_id'] = note_id
        if not valid_note(note_id):
            with self.db.transaction():
                self.event(progress, 'sample_skipped', reason='INVALID_NOTE_ID')
                self._finish_record(task, progress, 'invalid_id')
            return
        try:
            post = self.svc.source.post(progress['config_snapshot']['import_id'], note_id)
        except DomainError as error:
            if task['mode'] != 'auto' or error.code != 'SOURCE_NOT_FOUND':
                raise
            with self.db.transaction():
                self.event(progress, 'sample_skipped', reason='SOURCE_NOT_FOUND')
                self._finish_record(task, progress, 'missing_detail')
            return
        if task['mode'] == 'auto' and self._is_duplicate(task, note_id, post['post_key']):
            with self.db.transaction():
                self._finish_record(task, progress, 'duplicate')
            return
        self._read(progress, 'post_body', note_id=note_id)
        if post['body'] is None or post['quality']['body'] in ('failed', 'not_collected'):
            post = self.svc.source.post(progress['config_snapshot']['import_id'], note_id)
            self.event(progress, 'source_reread', source='post_body', note_id=note_id)
        if post['body'] is None or post['quality']['body'] in ('failed', 'not_collected'):
            self._ensure_record(task, progress, post)
            raise DomainError('NECESSARY_SOURCE_FAILED', '原帖必要正文不可用，重读后仍失败。', 503)
        result = self._pre_model(task, progress, 'classification:' + note_id,
                                 {'note_id': note_id, 'title': post['title'], 'body': post['body']})
        if result['payload']['type'] == 'non_rental':
            with self.db.transaction():
                self._finish_record(task, progress, 'not_rental')
            return
        self._ensure_record(task, progress, post)
        with self.db.transaction() as connection:
            if progress.get('current_sample'):
                progress['counters'][progress['current_sample']['region']]['eligible_count'] += 1
            result['input_refs'] = [{'note_id': note_id, 'parts': ['title', 'body']}]
            self._store_success(connection, task, 'classification', 'main', digest({'note_id': note_id}), result['payload'], result)
            self.event(progress, 'step_succeeded', stage='classification', type=result['payload']['type'], unknown_fallback=result['payload']['type'] == 'unknown')
            progress['current_step'] = 'body'
            self.save(task, progress)

    def _model_call(self, task, progress, stage, payload):
        if progress['config_snapshot'].get('prompt_version') != self.svc.prompt_version:
            raise DomainError('PROMPT_VERSION_UNAVAILABLE', '任务冻结的提示版本与当前程序不同，未静默更换规则或发送模型请求。', 503)
        calls = progress.get('model_call_count', 0)
        if calls >= self.svc.settings.model_call_limit:
            raise DomainError('MODEL_BUDGET_LIMIT', '任务模型调用预算已用完。', 503)
        progress['model_call_count'] = calls + 1
        progress['model_inflight'] = {'stage': stage, 'call_id': identifier('call'), 'input_hash': digest(payload), 'started_at': now()}
        call_id = progress['model_inflight']['call_id']
        with self.db.transaction():
            self.save(task, progress, 'running')
        try:
            result = self.model.invoke(stage, payload)
        finally:
            progress.pop('model_inflight', None)
        result['call_id'] = call_id
        try:
            result['payload'] = MODEL_SCHEMAS[stage].model_validate(result['payload']).model_dump()
        except (ValidationError, KeyError, TypeError):
            raise DomainError('AI_INVALID_OUTPUT', '模型输出未通过阶段结构校验。', 503, True) from None
        if task['record_id']:
            allowed = {row['evidence_id'] for row in self.db.all('SELECT evidence_id FROM evidence WHERE record_id=?', (task['record_id'],))}
            self._validate_evidence(result['payload'], allowed)
            if stage == 'summary':
                expected = {row['preference_id'] for row in self.db.all('SELECT preference_id FROM preferences WHERE profile_id=?', (task['profile_id'],))}
                actual = [item['preference_id'] for item in result['payload']['preferences']]
                if set(actual) != expected or len(actual) != len(expected):
                    raise DomainError('AI_INVALID_OUTPUT', '综合判断必须覆盖每项偏好且不重复。', 503, True)
                if any(item['result'] in ('pass', 'fail') and not item['evidence_ids'] for item in result['payload']['preferences']):
                    raise DomainError('AI_INVALID_OUTPUT', '有确定结论的偏好缺少真实证据。', 503, True)
                for topic in result['payload'].get('report_topics', []):
                    if topic['section'] == 'preference' and topic['preference_id'] not in expected:
                        raise DomainError('AI_INVALID_OUTPUT', '报告主题关联了不存在的偏好。', 503, True)
                    if topic['status'] == 'supported' and not topic['evidence_ids']:
                        raise DomainError('AI_INVALID_OUTPUT', '有确定结论的报告主题缺少证据。', 503, True)
                    if topic['status'] == 'disputed' and (len(topic['disagreements']) != 2 or any(not x['evidence_ids'] for x in topic['disagreements'])):
                        raise DomainError('AI_INVALID_OUTPUT', '报告分歧必须分别引用双方观点的证据。', 503, True)

        return result

    @staticmethod
    def _validate_evidence(value, allowed):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'evidence_ids' and (not isinstance(item, list) or not set(item) <= allowed):
                    raise DomainError('AI_INVALID_EVIDENCE', '模型引用了不存在或不属于本房源的证据。', 503, True)
                if key == 'evidence_ids':
                    value[key] = list(dict.fromkeys(item))
                Engine._validate_evidence(item, allowed)
        elif isinstance(value, list):
            for item in value:
                Engine._validate_evidence(item, allowed)

    def _pre_model(self, task, progress, scope, payload):
        cached = progress.setdefault('pre_steps', {}).get(scope)
        if cached:
            return cached
        for attempt in range(3):
            try:
                result = self._model_call(task, progress, 'classification', payload)
                self.guard(task['task_id'])
                progress['pre_steps'][scope] = result
                with self.db.transaction():
                    self.save(task, progress, 'running')
                return result
            except DomainError as error:
                if not error.retryable or attempt == 2:
                    raise
                self.sleep((1, 3)[attempt])

    def _store_success(self, connection, task, step, scope, input_hash, result, metadata=None):
        self.guard(task['task_id'])
        metadata = metadata or {}
        existing = self.db.one('SELECT * FROM steps WHERE record_id=? AND step_name=? AND scope_key=?', (task['record_id'], step, scope))
        logs = decode(existing['attempt_log']) if existing else []
        logs.append({'state': 'succeeded', 'at': now(), 'provider_request_id': metadata.get('provider_request_id'),
                     'usage': metadata.get('usage'), 'call_id': metadata.get('call_id', identifier('call'))})
        if metadata.get('report_presentation_version'):
            logs[-1]['report_presentation_version'] = metadata['report_presentation_version']
        connection.execute('INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id,step_name,scope_key) DO UPDATE SET state=excluded.state,input_refs=excluded.input_refs,input_hash=excluded.input_hash,result=excluded.result,model_version=excluded.model_version,prompt_version=excluded.prompt_version,attempt_log=excluded.attempt_log',
                           (existing['step_id'] if existing else identifier('step'), task['record_id'], step, scope, 'succeeded', encode(metadata.get('input_refs', decode(existing['input_refs']) if existing else [])), input_hash,
                            encode(result), metadata.get('model'), metadata.get('prompt_version'), encode(logs)))

    def _step(self, task, progress, step, scope, payload, call, model=False):
        existing = self.db.one('SELECT * FROM steps WHERE record_id=? AND step_name=? AND scope_key=?', (task['record_id'], step, scope))
        input_hash = digest(payload)
        input_refs = [{'evidence_id': item['evidence_id'], 'locator': item.get('locator')} for item in payload.get('evidence', [])]
        if 'target' in payload:
            input_refs.append({'community': payload.get('community'), 'target_poi': payload['target'].get('target_poi'),
                               'planning_context': payload.get('context')})
        if existing and existing['state'] == 'succeeded':
            # Locked profile, immutable batch and stage scope define successful reusable work.
            return decode(existing['result'])
        for attempt in range(3 if model else 1):
            with self.db.transaction() as connection:
                self.guard(task['task_id'])
                self.event(progress, 'step_started', stage=step, scope=scope)
                connection.execute('INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id,step_name,scope_key) DO UPDATE SET state=excluded.state,input_refs=excluded.input_refs,input_hash=excluded.input_hash',
                                   (existing['step_id'] if existing else identifier('step'), task['record_id'], step, scope, 'running', encode(input_refs), input_hash, None, None, None, '[]'))
                self.save(task, progress, 'running')
            try:
                output = call()
                result, metadata = (output['payload'], output) if model else (output, {})
                metadata['input_refs'] = input_refs
                with self.db.transaction() as connection:
                    self._store_success(connection, task, step, scope, input_hash, result, metadata)
                    self.event(progress, 'step_succeeded', stage=step, scope=scope)
                    self.save(task, progress, 'running')
                return result
            except (DomainError, AmapError) as error:
                with self.db.transaction() as connection:
                    self.guard(task['task_id'])
                    row = self.db.one('SELECT * FROM steps WHERE record_id=? AND step_name=? AND scope_key=?', (task['record_id'], step, scope))
                    log = decode(row['attempt_log'])
                    log.append({'state': 'failed', 'at': now(), 'code': error.code})
                    connection.execute("UPDATE steps SET state='failed',attempt_log=? WHERE step_id=?", (encode(log), row['step_id']))
                if not getattr(error, 'retryable', False) or attempt == (2 if model else 0):
                    raise
                self.sleep((1, 3)[attempt])

    def _map_cache(self, task, progress, endpoint, params, call):
        scope = digest({'endpoint': endpoint, 'params': params})
        cached = self.db.one("SELECT s.result FROM steps s JOIN records r ON r.record_id=s.record_id WHERE r.profile_id=? AND s.step_name='map_http' AND s.scope_key=? AND s.state='succeeded' LIMIT 1", (task['profile_id'], scope))
        if cached:
            return decode(cached['result'])
        if progress.get('map_call_count', 0) >= self.svc.settings.map_call_limit:
            raise DomainError('MAP_BUDGET_LIMIT', '本任务地图调用预算已用完，成功子调用已保留。', 503)
        progress['map_call_count'] = progress.get('map_call_count', 0) + 1
        progress['map_inflight'] = {'stage': 'map', 'call_id': identifier('call'), 'scope': scope, 'started_at': now()}
        call_id = progress['map_inflight']['call_id']
        with self.db.transaction():
            self.save(task, progress, 'running')
        try:
            result = call()
        finally:
            progress.pop('map_inflight', None)
        if str(result.get('status')) == '1':
            with self.db.transaction() as connection:
                self._store_success(connection, task, 'map_http', scope, scope, result, {'call_id': call_id, 'input_refs': [{'endpoint': endpoint, 'params': params}]})
                self.save(task, progress, 'running')
        self.guard(task['task_id'])
        return result

    def _evidence(self, task, post, text, locator, use, comment=None, author=None):
        evidence_key = digest({'post_key': post['post_key'] if post else None, 'comment': comment['comment_key'] if comment else None,
                               'author': author['author_key'] if author else None, 'locator': locator})
        with self.db.transaction() as connection:
            self.guard(task['task_id'])
            existing = self.db.one('SELECT * FROM evidence WHERE record_id=? AND evidence_key=?', (task['record_id'], evidence_key))
            if existing:
                uses = sorted(set(decode(existing['uses'])) | {use})
                connection.execute('UPDATE evidence SET uses=? WHERE evidence_id=?', (encode(uses), existing['evidence_id']))
                return {'evidence_id': existing['evidence_id'], 'text': existing['excerpt'], 'locator': locator}
            evidence_id = identifier('evidence')
            connection.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)',
                               (evidence_id, task['record_id'], post['post_key'] if post else None,
                                comment['comment_key'] if comment else None, author['author_key'] if author else None,
                                evidence_key, encode(locator), text, encode([use])))
            return {'evidence_id': evidence_id, 'text': text, 'locator': locator}

    def _condition(self, task, code, result, evidence_ids, route_context=None, preference_id=None):
        with self.db.transaction() as connection:
            self.guard(task['task_id'])
            existing = self.db.one('SELECT * FROM conditions WHERE record_id=? AND condition_code=?', (task['record_id'], code))
            if existing:
                return existing
            condition_id = identifier('condition')
            connection.execute('INSERT INTO conditions(condition_id,record_id,preference_id,condition_code,result,evidence_ids,route_context) VALUES(?,?,?,?,?,?,?)',
                               (condition_id, task['record_id'], preference_id, code, result, encode(evidence_ids), encode(route_context) if route_context else None))
            return self.db.one('SELECT * FROM conditions WHERE condition_id=?', (condition_id,))

    def _gate(self, task, progress, condition, message):
        if condition['result'] != 'fail' or condition['decision'] == 'continue':
            return True
        with self.db.transaction() as connection:
            if task['mode'] == 'auto':
                report_id = self.svc.screen(connection, task['record_id'], condition['condition_code'], message, step=progress['current_step'])
                progress['result']['brief_ids'].append(report_id)
                self.event(progress, 'record_screened', condition_id=condition['condition_id'])
                self._finish_record(task, progress, 'screened')
            else:
                progress['pending_action'] = {'kind': 'condition', 'record_id': task['record_id'],
                    'condition_id': condition['condition_id'], 'condition_version': condition['version'],
                    'prompt': message, 'allowed_actions': ['continue', 'abort'],
                    'route_context': decode(condition['route_context'])}
                self.event(progress, 'waiting_user', condition_id=condition['condition_id'])
                self.save(task, progress, 'waiting_user')
        return False

    def _route_gate(self, task, progress, route):
        routes = route.get('routes', [])
        if not routes:
            raise DomainError('ROUTE_UNCONFIRMED', '没有可靠的完整路线，技术问题不能豁免。', 503, True)
        if route.get('selected_route_id'):
            return route
        proposed = min(routes, key=lambda item: item['duration_sec'])
        context = {'route_policy_version': 'v1.37', 'evaluated_route_ids': [item['route_id'] for item in routes],
                   'proposed_route_id': proposed['route_id'], 'per_route_results': routes,
                   'waiver_scope': {'route_id': proposed['route_id']}}
        for code, passed, label in (('commute', proposed['commute_pass'], '拟采用路线的总通勤时间超过上限'),
                                     ('walk', proposed['walk_pass'], '拟采用路线的累计步行时间超过上限')):
            condition = self._condition(task, code, 'pass' if passed else 'fail', [], context)
            if not self._gate(task, progress, condition, label + '；请查看各方案，继续仅豁免当前条件。'):
                return None
        adopted = dict(route)
        adopted['adopted_route_id'] = proposed['route_id']
        adopted['waived_condition_ids'] = [row['condition_id'] for row in self.db.all("SELECT condition_id FROM conditions WHERE record_id=? AND decision='continue'", (task['record_id'],))]
        return adopted

    def _advance(self, task, progress):
        if not task['record_id']:
            self._classification(task, progress)
            return
        record = self.db.one('SELECT * FROM records WHERE record_id=?', (task['record_id'],))
        profile = self.db.one('SELECT * FROM profiles WHERE profile_id=?', (task['profile_id'],))
        post_row = self.db.one('SELECT note_id FROM posts WHERE post_key=?', (record['post_key'],))
        post = self.svc.source.post(profile['import_id'], post_row['note_id'])
        if not self.db.one("SELECT step_id FROM steps WHERE record_id=? AND step_name='classification' AND state='succeeded'", (task['record_id'],)):
            self._classification(task, progress)
            return
        body_step = self.db.one("SELECT * FROM steps WHERE record_id=? AND step_name='body' AND state='succeeded'", (task['record_id'],))
        if not body_step:
            progress['current_step'] = 'body'
            self._read(progress, 'post_body', note_id=post['note_id'])
            if post['body'] is None or post['quality']['body'] in ('failed', 'not_collected'):
                raise DomainError('NECESSARY_SOURCE_FAILED', '原帖正文重读后仍不可用。', 503)
            evidence = self._evidence(task, post, (post['title'] or '') + '\n' + post['body'], {'type': 'body', 'note_id': post['note_id']}, 'body')
            payload = {'title': post['title'], 'body': post['body'], 'evidence': [evidence]}
            self._step(task, progress, 'body', 'main', payload, lambda: self._model_call(task, progress, 'body', payload), True)
            with self.db.transaction():
                self.save(task, progress)
            return
        body = decode(body_step['result'])
        evidence_ids = [row['evidence_id'] for row in self.db.all("SELECT evidence_id FROM evidence WHERE record_id=? AND json_extract(locator,'$.type')='body'", (task['record_id'],))]
        for code, result, text in (
            ('city', 'fail' if body['city'] and body['city'].replace('市', '') != '深圳' else 'pass' if body['city'] else 'unknown', '帖子明确属于其他城市'),
            ('min_rent', 'unknown' if body['rent_cent'] is None else 'fail' if body['rent_cent'] < profile['min_rent_cent'] else 'pass', '正文月租低于预算下限'),
            ('max_rent', 'unknown' if body['rent_cent'] is None else 'fail' if body['rent_cent'] > profile['max_rent_cent'] else 'pass', '正文月租高于预算上限')):
            progress['current_step'] = 'body_gate'
            condition = self._condition(task, code, result, evidence_ids)
            if not self._gate(task, progress, condition, text):
                return
        community = record['community_supplement'] or body['community_name']
        if not community:
            progress['current_step'] = 'community_prompt'
            with self.db.transaction():
                progress['pending_action']={'kind':'community','record_id':task['record_id'],'prompt':'原帖未提供可定位小区，请补充完整地址或跳过。','allowed_actions':['submit','skip']}
                self.save(task,progress,'waiting_user')
            return
        progress['current_step'] = 'map'
        snapshot = progress['config_snapshot']
        map_payload = {'community': community, 'target': snapshot['target'], 'context': snapshot['planning_context'],
                       'commute_limit_sec': profile['commute_limit_sec'], 'walk_limit_sec': profile['walk_limit_sec']}
        confirmation=progress.get('location_confirmations',{}).get(task['record_id'])
        refs=[confirmation] if confirmation else post['community_refs']
        if confirmation:map_payload['confirmed_location']=confirmation
        route = self._step(task, progress, 'map', 'main', map_payload,
            lambda: self.maps.evaluate(community, refs, snapshot['target'], profile['commute_limit_sec'],
                profile['walk_limit_sec'], snapshot['planning_context'],
                cache_call=lambda endpoint, params, call: self._map_cache(task, progress, endpoint, params, call)))
        adopted = self._route_gate(task, progress, route)
        if adopted is None:
            return
        # Only code below this point may read comments, author profiles or auxiliary posts.
        phases = [('comments', 'main'), ('author', 'main'), ('community', 'main')]
        preferences = self.db.all('SELECT * FROM preferences WHERE profile_id=? ORDER BY display_order', (task['profile_id'],))
        phases += [('preference', item['preference_id']) for item in preferences if item['kind'] == 'custom']
        for stage, scope in phases:
            existing = self.db.one("SELECT * FROM steps WHERE record_id=? AND step_name=? AND scope_key=? AND state='succeeded'", (task['record_id'], stage, scope))
            if existing:
                continue
            progress['current_step'] = stage
            with self.db.transaction():
                self.save(task,progress,'running')
            available=progress['config_snapshot'].get('auxiliary_policy')=='available_evidence_v1'
            if available:
                from .auxiliary import collect
                payload=collect(self,task,progress,post,community,stage,scope,preferences)
            else:payload = self._investigation_input(task, progress, post, community, stage, scope, preferences)
            if not payload['evidence'] or payload.get('skip_model'):
                result = {'facts': [], 'missing': payload['missing'], 'conflicts': [], 'identity_hint': '无法确认' if stage == 'author' else None}
                if available:
                    from .auxiliary import annotate
                    result=annotate(result,payload)
                with self.db.transaction() as connection:
                    self._store_success(connection, task, stage, scope, digest(payload), result, {'input_refs': [{'note_id': post['note_id'], 'community': community, 'preference': payload['preference'], 'source_state': 'empty_or_unavailable_as_recorded'}]})
            else:
                def analyze():
                    result=self._model_call(task,progress,stage,payload)
                    if available:
                        from .auxiliary import annotate
                        result['payload']=annotate(result['payload'],payload)
                    return result
                try:self._step(task, progress, stage, scope, payload, analyze, True)
                except DomainError as e:
                    from .service import GLOBAL_TASK_ERRORS
                    if not available or e.code in GLOBAL_TASK_ERRORS or e.code in ('PROVIDER_CONFIGURATION_ERROR','MODEL_INPUT_LIMIT') or e.code.startswith('DATABASE'):raise
                    from .auxiliary import annotate
                    payload={**payload,'collection_status':'failed','warnings':payload['warnings']+[e.code],
                        'sources':payload['sources']+[{'source_kind':stage+'_analysis','source_ref':scope,'collection_status':'failed','reason':e.message,'impact':'辅助分析失败，保留真实原文供S6综合，不作为筛除依据。'}]}
                    result=annotate({'facts':[],'conflicts':[],'missing':['辅助分析失败：'+e.message],'identity_hint':'无法确认' if stage=='author' else None},payload)
                    with self.db.transaction() as connection:
                        self._store_success(connection,task,stage,scope,digest(payload),result)
                        self.event(progress,'auxiliary_degraded',stage=stage,code=e.code)
            with self.db.transaction():
                self.save(task, progress)
            return
        progress['current_step'] = 'summary'
        all_evidence = self.db.all('SELECT evidence_id,excerpt,locator,uses FROM evidence WHERE record_id=? ORDER BY evidence_id', (task['record_id'],))
        evidence = [{**item, 'locator': decode(item['locator']), 'uses': decode(item['uses'])} for item in all_evidence]
        facts = [dict(stage=row['step_name'], scope=row['scope_key'], result=decode(row['result'])) for row in
                 self.db.all("SELECT * FROM steps WHERE record_id=? AND step_name IN('body','comments','author','community','preference') AND state='succeeded' ORDER BY step_name,scope_key", (task['record_id'],))]
        unique_facts = {}
        channels = []
        for investigation in facts:
            result = investigation['result']
            channels.append({'stage': investigation['stage'], 'scope': investigation['scope'],
                             'missing': result.get('missing', []), 'identity_hint': result.get('identity_hint'),
                             'collection_status':result.get('collection_status'),'sources':result.get('sources',[]),'warnings':result.get('warnings',[])})
            for kind in ('facts', 'conflicts'):
                for fact in result.get(kind, []):
                    key = digest({'text': fact['text'], 'evidence_ids': sorted(set(fact['evidence_ids']))})
                    normalized = unique_facts.setdefault(key, {**fact, 'uses': []})
                    normalized['uses'].append({'stage': investigation['stage'], 'scope': investigation['scope'], 'kind': kind})
        payload = {'preferences': preferences, 'evidence': evidence, 'facts': list(unique_facts.values()),
                   'channels': channels, 'body': body, 'preference_policy': PREFERENCE_POLICY}
        data_completeness=None
        if progress['config_snapshot'].get('auxiliary_policy')=='available_evidence_v1':
            from .auxiliary import completeness
            data_completeness=completeness(facts)
            payload.update(auxiliary_policy='available_evidence_v1',data_completeness=data_completeness)
        summary = self._step(task, progress, 'summary', 'main', payload, lambda: self._model_call(task, progress, 'summary', payload), True)
        if data_completeness:
            summary={**summary,'preferences':[dict(item) for item in summary['preferences']],
                'missing':list(dict.fromkeys(summary.get('missing',[])+[s['reason'] for s in data_completeness['missing'] if s.get('reason')]))}
            for item in summary['preferences']:
                item['missing_source_kinds']=sorted({s['source_kind'] for s in data_completeness['missing']})
                item['non_blocking_reason']='no_explicit_violation_with_insufficient_evidence' if item['result']=='unknown' else ('flexible_preference' if next(p for p in preferences if p['preference_id']==item['preference_id'])['requirement']=='flexible' and item['result']=='fail' else None)
        for preference in preferences:
            result = next(item for item in summary['preferences'] if item['preference_id'] == preference['preference_id'])
            condition = self._condition(task, 'pref:' + preference['preference_id'], result['result'], result['evidence_ids'], preference_id=preference['preference_id'])
            if preference['requirement'] == 'must' and not self._gate(task, progress, condition, preference['original_text'] + '：' + result['reason']):
                return
        with self.db.transaction() as connection:
            self.guard(task['task_id'])
            existing = self.db.one("SELECT * FROM reports WHERE record_id=? AND kind='full'", (task['record_id'],))
            if not existing:
                content = {**summary, 'schema_version': 1, 'body': body, 'route': adopted,
                           'report_presentation_version': 'synthesis-v1' if summary.get('report_topics') else 'legacy-summary',
                           'evidence_ids': [item['evidence_id'] for item in evidence],
                           'conditions': [self.svc.condition_public(item) for item in self.db.all('SELECT * FROM conditions WHERE record_id=?', (task['record_id'],))],
                           'investigations': facts}
                content['risks'] = [{**item, 'risk_id': identifier('risk')} for item in summary['risks']]
                if data_completeness:content['data_completeness']=data_completeness
                connection.execute("INSERT INTO reports VALUES(?,?,'full',?,?,?)", (identifier('report'), task['record_id'], encode(content), digest(content), now()))
                selected_id = adopted.get('selected_route_id') or adopted.get('adopted_route_id')
                selected = next(item for item in adopted['routes'] if item['route_id'] == selected_id)
                connection.execute("UPDATE records SET list_status='candidate',manual_rent_cent=?,manual_monthly_cent=?,manual_initial_cent=?,manual_commute_sec=?,candidate_added_at=?,version=version+1 WHERE record_id=?",
                                   (body['rent_cent'], body.get('monthly_cent'), body.get('initial_cent'), selected['duration_sec'], now(), task['record_id']))
                if task['mode'] == 'auto':
                    progress['counters'][progress['current_sample']['region']]['candidate_added_count'] += 1
                self.event(progress, 'candidate_added', record_id=task['record_id'])
            self._finish_record(task, progress, 'completed')

    def _investigation_input(self, task, progress, post, community, stage, scope, preferences):
        evidence, missing = [], []
        if stage == 'comments':
            self._read(progress, 'comments', note_id=post['note_id'])
            q = post['quality']['primary_comments']
            rows = self.svc.source.comments(post['post_key'])
            if q in ('failed', 'not_collected') or (not rows and q != 'empty_confirmed'):
                self.svc.source.comments(post['post_key'])  # One explicit source reread; never an online fetch.
                raise DomainError('NECESSARY_SOURCE_FAILED', '原帖必要一级评论重读后仍不可用。', 503)
            for comment in rows:
                evidence.append(self._evidence(task, post, comment['body'], {'type': 'comment', 'comment_id': comment['comment_id']}, stage, comment=comment))
            if post['quality']['replies'] != 'success':
                missing.append('二级回复完整性未确认，保留已取得评论。')
        elif stage == 'author':
            self._read(progress, 'author', author_key=post['author_key'])
            author = self.svc.source.author(post['author_key'])
            if not author or author['quality']['status'] in ('failed', 'not_collected'):
                self.svc.source.author(post['author_key'])
                raise DomainError('NECESSARY_SOURCE_FAILED', '作者必要资料重读后仍不可用，不能将未采集当空历史。', 503)
            if author['quality']['status']=='partial':missing.append('作者资料仅部分取得，历史标题不完整；不能认定已查全或没有其他帖子。')
            if author.get('nickname') or author.get('profile_text'):
                text = (author.get('nickname') or '') + '\n' + (author.get('profile_text') or '')
                evidence.append(self._evidence(task, None, text, {'type': 'author_profile', 'author_id': author['author_id']}, stage, author=author))
            for index, title in enumerate(author['history_titles']):
                if title.get('title'):
                    evidence.append(self._evidence(task, None, title['title'], {'type': 'history_title', 'index': index,
                        'note_id': title.get('note_id'), 'url': title.get('original_url')}, stage, author=author))
        else:
            preference = next((item['original_text'] for item in preferences if item['preference_id'] == scope), None)
            self._read(progress, stage, community=community, preference=preference)
            posts = self.svc.source.auxiliary(post['import_id'], community, preference)
            for item in posts:
                if item['quality']['body'] in ('failed', 'not_collected') or item['body'] is None:
                    item = self.svc.source.post(post['import_id'], item['note_id'])
                    self.event(progress, 'source_reread', source='auxiliary_body', note_id=item['note_id'])
                if item['quality']['body'] in ('failed', 'not_collected') or item['body'] is None:
                    missing.append('辅助帖子正文读取失败：' + item['note_id'])
                    continue
                evidence.append(self._evidence(task, item, (item['title'] or '') + '\n' + item['body'], {'type': 'body', 'note_id': item['note_id']}, stage))
                if item['quality']['primary_comments'] in ('failed', 'not_collected'):
                    self.svc.source.comments(item['post_key'])
                    missing.append('辅助评论读取失败：' + item['note_id'])
                    continue
                for comment in self.svc.source.comments(item['post_key']):
                    evidence.append(self._evidence(task, item, comment['body'], {'type': 'comment', 'comment_id': comment['comment_id']}, stage, comment=comment))
            if not posts:
                missing.append('原文精确检索正常0篇；不能单凭此判断偏好符合。')
        return {'community': community, 'preference': next((item['original_text'] for item in preferences if item['preference_id'] == scope), None),
                'evidence': list({item['evidence_id']: item for item in evidence}.values()), 'missing': missing}


