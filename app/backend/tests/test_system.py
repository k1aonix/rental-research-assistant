"""Isolated contract/flow tests. Artificial providers exist only in this test directory."""
import json
import threading
import tempfile
import sqlite3
from pathlib import Path
import unittest
from collections import Counter
from datetime import datetime

from starlette.testclient import TestClient

from rental.api import create_app
from rental.common import DomainError, decode
from rental.config import Settings
from rental.db import Database
from rental.source import SourceImporter


AUTHOR = 'a' * 24


def nid(value):
    return f'{value:024x}'


def dataset(notes=1, positions=None, rent=300000, community='测试小区', comments_status='success', author_status='success'):
    rows = [{'note_id': nid(i), 'original_url': 'https://www.xiaohongshu.com/explore/' + nid(i), 'title': '出租测试安静' + str(i),
             'body': json.dumps({'city': '深圳', 'rent_cent': rent, 'community_name': community}, ensure_ascii=False),
             'author_id': AUTHOR, 'author_nickname': '测试发布者', 'status': 'success',
             'comments_status': comments_status, 'primary_comments_status': comments_status, 'replies_status': 'success'} for i in range(1, notes+1)]
    if positions is None:
        positions = [nid(i) for i in range(1, notes+1)]
    data = {'schema_version': 1, 'notes': rows,
            'comments': [{'note_id': row['note_id'], 'comment_id': 'comment-' + row['note_id'], 'parent_comment_id': None, 'body': '测试居住体验：安静。'} for row in rows],
            'search_runs': [{'search_id': 'near-school', 'round_id': 1, 'keyword': '深圳 港中深 租房', 'status': 'success' if positions else 'empty'}],
            'search_results': [{'search_id': 'near-school', 'position': index, 'note_id': value, 'status': 'success' if value else 'partial'} for index, value in enumerate(positions, 1)],
            'author_history_runs': [{'author_id': AUTHOR, 'status': author_status, 'profile_text': '测试账户资料'}],
            'author_history': [] if author_status == 'not_attempted' else [{'author_id': AUTHOR, 'note_id': nid(100), 'title': '测试历史标题', 'original_url': 'https://www.xiaohongshu.com/explore/' + nid(100)}]}
    manifest = {'search_mappings': {'near-school': {'discovery_scope_verified': True,'discovery_scope':{'mode':'walking','limit_sec':1200,'target_poi':'B0FFF4H4MQ','target_location':'114.212820,22.688319','anchor':{'poi_id':'isolated-fixture-area','location':'114.212820,22.689319','walk_sec':1140},'evidence_file':'isolated-test-provider-only'}}}, 'coverage': {'near_metro': 'empty_confirmed'}}
    return data, manifest


class ModelStub:
    def __init__(self):
        self.calls = Counter()
        self.fail = Counter()
        self.preference_result = 'unknown'
        self.bad_reference = False
        self.non_rental = set()
        self.reset_callback = None

    def ready(self):
        return True

    def invoke(self, stage, payload):
        self.calls[stage] += 1
        if self.reset_callback:
            callback, self.reset_callback = self.reset_callback, None
            callback()
        if self.fail[stage]:
            self.fail[stage] -= 1
            raise DomainError('MODEL_NETWORK_ERROR', '隔离测试故障', 503, True)
        refs = [item['evidence_id'] for item in payload.get('evidence', [])]
        if stage == 'classification':
            result = {'type': 'non_rental' if payload['note_id'] in self.non_rental else 'rental', 'reason': '隔离测试分类'}
        elif stage == 'body':
            source = json.loads(payload['body'])
            result = {**source, 'monthly_cent': None, 'initial_cent': None, 'facts': [{'text': '测试原文事实', 'evidence_ids': refs}]}
        elif stage == 'summary':
            result = {'summary': '隔离测试综合结论', 'facts': [], 'conflicts': [], 'missing': [],
                      'preferences': [{'preference_id': item['preference_id'], 'result': self.preference_result,
                                       'reason': '隔离测试偏好判断', 'evidence_ids': refs[:1] if self.preference_result != 'unknown' else []}
                                      for item in payload['preferences']],
                      'risks': [{'title': '测试风险' + str(i), 'reason': '来自测试资料', 'evidence_ids': refs[:1], 'verification_hint': '现场核实'} for i in range(5)]}
        else:
            result = {'facts': [{'text': '测试取证', 'evidence_ids': ['not-existing'] if self.bad_reference else refs[:1]}] if refs else [],
                      'conflicts': [], 'missing': [], 'identity_hint': '疑似中介' if stage == 'author' else None}
        return {'payload': result, 'model': 'isolated-test-provider', 'prompt_version': 'test-v1.37', 'provider_request_id': f'test-{stage}-{self.calls[stage]}', 'usage': {}}


class MapStub:
    def __init__(self, routes=None):
        self.calls = Counter()
        self.routes = routes or [(1800, 540)]
        self.fail_once = False
        self.failures = 0
        self.station_budgets = []

    def ready(self):
        return True

    def evaluate(self, community, refs, target, commute, walk, context, cache_call=None):
        self.calls['evaluate'] += 1
        if cache_call:
            def raw():
                self.calls['raw_walking'] += 1
                return {'status': '1', 'route': {'paths': []}}
            cache_call('walking', {'date': context['reference_date']}, raw)
        if self.fail_once and not self.failures:
            self.failures += 1
            raise DomainError('MAP_NETWORK_ERROR', '隔离地图故障', 503, True)
        options = [{'route_id': 'route_' + str(index), 'mode': 'bus' if index % 2 else 'bus_metro', 'duration_sec': total,
                    'total_walk_sec': walking, 'walking_distance_m': 500, 'transfer_count': index % 2,
                    'commute_pass': total <= commute, 'walk_pass': walking <= walk, 'joint_pass': total <= commute and walking <= walk,
                    'segments': [], 'quality': 'success'} for index, (total, walking) in enumerate(self.routes)]
        eligible = [item for item in options if item['joint_pass']]
        selected = min(eligible, key=lambda item: item['duration_sec']) if eligible else None
        return {'schema_version': 1, 'route_policy_version': 'v1.37', **context, 'queried_at': '2026-11-16T12:00:00+00:00',
                'routes': options, 'selected_route_id': selected['route_id'] if selected else None,
                'commute_limit_sec': commute, 'walk_limit_sec': walk, 'quality': 'success'}

    def station_plan(self, target, commute, context, cache_call=None):
        self.calls['station_plan'] += 1
        self.station_budgets.append(commute)
        return {'stations': [], 'quality': 'success', 'planning_context': context}


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.settings = Settings(database=':memory:', reference_date='2026-11-17', planning_context_verified=True,
                                 requests_per_minute=10000, task_limit_per_day=1000)
        self.model, self.maps = ModelStub(), MapStub()
        self.app = create_app(self.settings, model=self.model, maps=self.maps, database=self.db, run_worker=False)
        self.app.state.engine.sleep = lambda _: None
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.key = 0
        self.workspace = None

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.close()

    def write(self, path, payload, method='POST', key=None, expected=200):
        self.key += 1
        headers = {'Origin': 'http://testserver', 'Idempotency-Key': key or str(self.key),
                   'X-CSRF-Token': self.workspace['csrf_token'] if self.workspace else ''}
        response = self.client.request(method, '/api/v1' + path, json=payload, headers=headers)
        self.assertIn(response.status_code, (expected,) if expected != 200 else (200, 201, 202), response.text)
        return response.json()

    def prepare(self, **kwargs):
        data, manifest = dataset(**kwargs)
        SourceImporter(self.db).import_data(data, manifest, 'test-source')
        self.workspace = self.write('/workspace', {})['data']
        profile = self.workspace['profile']
        self.write('/profile/confirm-target', {'profile_id': profile['profile_id'], 'version': profile['version'],
                    'config_hash': self.workspace['fixed_config']['config_hash']})
        self.refresh()
        self.lock_profile()
        return data, manifest

    def refresh(self):
        self.workspace = self.client.get('/api/v1/workspace').json()['data']
        return self.workspace['profile']

    def command(self, version=None):
        profile = self.workspace['profile']
        return {'profile_id': profile['profile_id'], 'version': profile['version'] if version is None else version}

    def lock_profile(self, commute=40, walk=10, preferences=None):
        self.write('/profile/save', {**self.command(), 'min_rent_cent': 200000, 'max_rent_cent': 400000,
                    'max_commute_min': commute, 'max_walk_min': walk,
                    'preferences': preferences or [{'kind': 'agent', 'original_text': '拒绝中介及二房东', 'requirement': 'must'},
                                                   {'kind': 'custom', 'original_text': '安静', 'requirement': 'must'}]})
        self.refresh()

    def manual(self, note=1):
        return self.write('/tasks/import', {**self.command(), 'url': 'https://www.xiaohongshu.com/explore/' + nid(note)})['data']

    def drain(self, task_id, limit=200):
        for _ in range(limit):
            task = self.client.get('/api/v1/tasks/' + task_id).json()['data']
            if task['state'] in ('completed', 'failed', 'waiting_user', 'cancelled'):
                return task
            self.app.state.engine.advance(task_id)
        self.fail('Task did not settle')

    def record(self, task):
        record_id = task['result']['record_ids'][0]
        return self.client.get('/api/v1/records/' + record_id).json()['data']

    def test_manual_full_report_and_non_model_operations(self):
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        record = self.record(task)
        self.assertEqual(record['list_status'], 'candidate')
        self.assertEqual(record['manual_values']['monthly_cent'], None)
        original_calls = self.model.calls.copy(), self.maps.calls.copy()
        report = self.client.get('/api/v1/reports/' + record['full_report_id']).json()['data']
        changed = self.write('/records/' + record['record_id'], {**self.command(record['version']), 'group': 'cost',
                    'rent_cent': 100, 'monthly_cent': None, 'initial_cent': None}, 'PATCH')['data']
        archived = self.write('/records/archive', {**self.command(), 'items': [{'record_id': record['record_id'], 'version': changed['version']}], 'confirm': True})['data']['records'][0]
        restored = self.write('/records/' + record['record_id'] + '/restore', self.command(archived['version']))['data']
        self.assertEqual(restored['manual_values']['rent_cent'], 100)
        self.assertEqual(self.client.get('/api/v1/reports/' + record['full_report_id']).json()['data'], report)
        self.assertEqual((self.model.calls, self.maps.calls), original_calls)
        repeated = self.manual()
        self.assertEqual(repeated['disposition'], 'existing')
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM reports WHERE kind='full'")['n'], 1)

    def test_auto_hard_failure_never_reads_downstream(self):
        self.prepare(rent=500000)
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        record = self.record(task)
        self.assertEqual(record['list_status'], 'screened')
        reads = [event['data']['source'] for event in task['events'] if event['type'] == 'source_read']
        self.assertEqual(set(reads), {'post_body'})
        self.assertEqual(self.maps.calls['evaluate'], 0)
        self.assertEqual(task['counters']['near_target']['eligible_count'], 1)
        self.assertEqual(task['counters']['near_target']['candidate_added_count'], 0)

    def test_independent_commute_and_walk_waivers(self):
        self.prepare()
        self.maps.routes = [(3000, 1200)]
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['pending_action']['kind'], 'condition', task)
        self.assertEqual(task['pending_action']['route_context']['proposed_route_id'], 'route_0')
        self.assertEqual(self.model.calls['comments'], 0)
        for expected_code in ('commute', 'walk'):
            cid = task['pending_action']['condition_id']
            condition = self.db.one('SELECT * FROM conditions WHERE condition_id=?', (cid,))
            self.assertEqual(condition['condition_code'], expected_code)
            task = self.write('/conditions/' + cid + '/decision', {**self.command(condition['version']), 'decision': 'continue'})['data']['task']
            task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.record(task)['list_status'], 'candidate')
        conditions = self.db.all("SELECT * FROM conditions WHERE condition_code IN('commute','walk')")
        self.assertTrue(all(item['decision'] == 'continue' and item['result'] == 'fail' for item in conditions))
        self.assertEqual(self.maps.calls['evaluate'], 1)

    def test_cross_route_conflict_is_not_joint_pass(self):
        self.prepare()
        self.maps.routes = [(1500, 900), (3000, 480)]
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(self.record(task)['list_status'], 'screened')
        self.assertEqual(self.model.calls['comments'], 0)

    def test_auto_sampling_duplicates_invalid_non_rental_and_quotas(self):
        positions = [nid(1), nid(1), None, nid(2), *[nid(i) for i in range(3, 12)]]
        self.prepare(notes=11, positions=positions)
        self.model.non_rental.add(nid(2))
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        counts = task['counters']['near_target']
        self.assertEqual(counts['hit_count'], len(positions))
        self.assertEqual(counts['eligible_count'], 5)
        self.assertEqual(counts['attempt_count'], 8)
        self.assertEqual(counts['candidate_added_count'], 5)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM records')['n'], 5)

    def test_original_comments_failure_is_non_blocking_and_visible(self):
        self.prepare(comments_status='failed')
        task=self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'],'completed');self.assertEqual(self.record(task)['list_status'],'candidate')
        full=self.db.one("SELECT content FROM reports WHERE kind='full'");quality=decode(full['content'])['data_completeness']
        self.assertTrue(any(s['source_kind']=='original_comments' and s['collection_status']=='failed' for s in quality['sources']))
        self.assertFalse(task['pending_action'])

    def test_ai_failure_retries_only_failed_stage(self):
        self.prepare()
        self.model.fail['summary'] = 3
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'], 'failed', task)
        self.assertEqual(self.model.calls['classification'], 1)
        self.write('/tasks/' + task['task_id'] + '/retry', self.command(task['version']))
        task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.model.calls['summary'], 4)
        self.assertEqual(self.model.calls['classification'], 1)
        self.assertEqual(self.model.calls['body'], 1)
        self.assertEqual(self.maps.calls['evaluate'], 1)

    def test_map_retry_reuses_successful_http_subcall(self):
        self.prepare()
        self.maps.fail_once = True
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'], 'failed', task)
        self.write('/tasks/' + task['task_id'] + '/retry', self.command(task['version']))
        task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.maps.calls['raw_walking'], 1)
        self.assertEqual(self.model.calls['body'], 1)

    def test_author_agent_hint_does_not_screen_before_summary(self):
        self.prepare()
        self.model.preference_result = 'fail'
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(self.record(task)['list_status'], 'screened')
        self.assertEqual(self.model.calls['author'], 1)
        self.assertEqual(self.model.calls['community'], 0)
        self.assertEqual(self.model.calls['preference'], 0)
        self.assertEqual(self.model.calls['summary'], 1)

    def test_unknown_preferences_are_candidates_and_evidence_is_deduplicated(self):
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(self.record(task)['list_status'], 'candidate')
        refs = self.db.all('SELECT evidence_key,uses FROM evidence')
        self.assertEqual(len(refs), len({item['evidence_key'] for item in refs}))
        self.assertEqual(self.model.calls['preference'],0)
        self.assertTrue(all(len(decode(item['uses']))==len(set(decode(item['uses']))) for item in refs))

    def test_viewing_atomic_keys_progress_and_no_automatic_archive(self):
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        record = self.record(task)
        v = self.client.get('/api/v1/records/' + record['record_id'] + '/viewing').json()['data']
        body = {**self.command(0), 'conclusion': 'reject', 'summary': '现场记录', 'followup': '追问',
                'general_checks': {key: index < 8 for index, key in enumerate(v['general_checks'])},
                'risk_checks': {key: index < 3 for index, key in enumerate(v['risk_checks'])}, 'template_version': v['template']['template_version']}
        saved = self.write('/records/' + record['record_id'] + '/viewing', body, 'PUT')['data']
        self.assertEqual(saved['viewing']['progress'], {'done': 11, 'total': 17})
        self.assertEqual(saved['record']['risk_done'], 3)
        self.assertEqual(saved['record']['list_status'], 'candidate')
        body['version'] = 1
        body['general_checks'] = {}
        self.write('/records/' + record['record_id'] + '/viewing', body, 'PUT', expected=422)
        self.assertEqual(self.db.one('SELECT summary FROM viewing')['summary'], '现场记录')

    def test_anonymous_isolation_and_csrf(self):
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        record = self.record(task)
        with TestClient(self.app) as other:
            init = other.post('/api/v1/workspace', json={}, headers={'Origin': 'http://testserver', 'Idempotency-Key': 'other'})
            self.assertEqual(init.status_code, 201)
            self.assertEqual(other.get('/api/v1/records/' + record['record_id']).status_code, 404)
            self.assertEqual(other.get('/api/v1/tasks/' + task['task_id']).status_code, 404)
            self.assertEqual(other.get('/api/v1/reports/' + record['full_report_id']).status_code, 404)
        response = self.client.post('/api/v1/tasks/search', json=self.command(), headers={'Origin': 'https://evil.invalid', 'Idempotency-Key': 'evil'})
        self.assertEqual(response.status_code, 403)

    def test_idempotency_and_reset_replay(self):
        self.prepare()
        data = {**self.command(), 'url': 'https://www.xiaohongshu.com/explore/' + nid(1)}
        first = self.write('/tasks/import', data, key='same')['data']
        second = self.write('/tasks/import', data, key='same')['data']
        self.assertEqual(first['task_id'], second['task_id'])
        data['url'] = 'https://www.xiaohongshu.com/explore/' + nid(2)
        self.write('/tasks/import', data, key='same', expected=409)
        body = {**self.command(), 'old_profile_id': self.command()['profile_id'], 'confirm': True}
        first_reset = self.write('/profile/reset', body, key='reset')['data']
        second_reset = self.write('/profile/reset', body, key='reset')['data']
        self.assertEqual(first_reset['profile']['profile_id'], second_reset['profile']['profile_id'])
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM tasks')['n'], 0)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM posts')['n'], 1)

    def test_late_model_result_cannot_write_after_reset(self):
        self.prepare()
        task_id = self.manual()['task_id']
        old = self.command()
        self.model.reset_callback = lambda: self.write('/profile/reset', {**old, 'old_profile_id': old['profile_id'], 'confirm': True})
        self.app.state.engine.advance(task_id)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM records')['n'], 0)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM steps')['n'], 0)

    def test_manual_community_supplement_does_not_reclassify(self):
        self.prepare(community=None)
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['pending_action']['kind'], 'community', task)
        record = self.record(task)
        self.write('/records/' + record['record_id'] + '/community', {**self.command(record['version']), 'name': '测试小区'})
        task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.model.calls['body'], 1)
        self.assertEqual(self.model.calls['classification'], 1)

    def test_invalid_urls_and_source_miss_do_not_call_ai(self):
        self.prepare()
        for url, status in [('https://evil.invalid/explore/' + nid(1), 422),
                            ('https://www.xiaohongshu.com/explore/' + nid(20), 404), ('https://xhslink.com/a/abc', 422)]:
            self.write('/tasks/import', {**self.command(), 'url': url}, expected=status)
        self.assertEqual(self.model.calls, {})
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM records')['n'], 0)

    def test_no_legacy_fields_and_openapi_generated_from_models(self):
        self.prepare()
        response = self.write('/tasks/search', {**self.command(), 'metro_walk_limit_sec': 600}, expected=422)
        self.assertEqual(response['error']['code'], 'VALIDATION_FAILED')
        schema = self.client.get('/openapi.json').json()
        self.assertIn('/api/v1/records/{id}/viewing', schema['paths'])
        self.assertIn('max_walk_min', schema['components']['schemas']['SaveProfile']['properties'])
        self.assertNotIn('metro_walk_limit_sec', schema['components']['schemas']['SaveProfile']['properties'])

    def test_zero_results_and_unknown_source_coverage_are_distinct(self):
        self.prepare(positions=[])
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(task['result']['outcome'], 'no_hits', task)
        self.assertEqual(task['counters']['near_target']['attempt_count'], 0)
        self.assertEqual(self.model.calls, {})

    def test_batch_archive_rolls_back_and_versions_conflict(self):
        self.prepare(notes=2)
        a, b = self.record(self.drain(self.manual(1)['task_id'])), self.record(self.drain(self.manual(2)['task_id']))
        self.write('/records/archive', {**self.command(), 'confirm': True, 'items': [
            {'record_id': a['record_id'], 'version': a['version']}, {'record_id': b['record_id'], 'version': 999}]}, expected=409)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM records WHERE list_status=?', ('archived',))['n'], 0)

    def test_restart_recovery_keeps_successful_steps_and_sample_counters(self):
        self.prepare(notes=2)
        task_id = self.write('/tasks/search', self.command())['data']['task_id']
        self.app.state.engine.advance(task_id)  # classification completes, record reserved
        task = self.client.get('/api/v1/tasks/' + task_id).json()['data']
        count = task['counters']['near_target']['attempt_count']
        with self.db.transaction() as connection:
            connection.execute("UPDATE tasks SET state='running' WHERE task_id=?", (task_id,))
        self.app.state.engine.recover()
        done = self.drain(task_id)
        self.assertEqual(done['state'], 'completed', done)
        self.assertEqual(done['counters']['near_target']['attempt_count'], 2)
        self.assertEqual(count, 1)
        self.assertEqual(self.model.calls['classification'], 2)

    def test_restart_unknown_provider_outcome_requires_explicit_retry(self):
        self.prepare()
        task_id = self.manual()['task_id']
        row = self.db.one('SELECT * FROM tasks WHERE task_id=?', (task_id,))
        progress = decode(row['progress'])
        progress['model_inflight'] = {'stage': 'classification', 'call_id': 'uncertain-call'}
        with self.db.transaction() as connection:
            connection.execute("UPDATE tasks SET state='running',progress=? WHERE task_id=?", (json.dumps(progress), task_id))
        self.app.state.engine.recover()
        task = self.client.get('/api/v1/tasks/' + task_id).json()['data']
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['error']['code'], 'PROVIDER_OUTCOME_UNKNOWN')
        self.assertEqual(self.model.calls, {})

    def test_waiting_user_releases_executor_for_other_property(self):
        self.prepare(notes=2)
        self.maps.routes = [(3000, 900)]
        waiting = self.drain(self.manual(1)['task_id'])
        self.assertEqual(waiting['state'], 'waiting_user')
        self.maps.routes = [(1200, 100)]
        second = self.drain(self.manual(2)['task_id'])
        self.assertEqual(second['state'], 'completed', second)
        self.assertEqual(self.client.get('/api/v1/tasks/' + waiting['task_id']).json()['data']['state'], 'waiting_user')

    def test_single_executor_and_cancelled_late_result(self):
        self.prepare()
        task_id = self.manual()['task_id']
        entered, release = threading.Event(), threading.Event()
        def block_provider():
            entered.set()
            release.wait(5)
        self.model.reset_callback = block_provider
        worker = threading.Thread(target=lambda: self.app.state.engine.advance(task_id))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.app.state.engine.advance(task_id))
            task = self.client.get('/api/v1/tasks/' + task_id).json()['data']
            self.write('/tasks/' + task_id + '/cancel', {**self.command(task['version']), 'confirm': True})
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.client.get('/api/v1/tasks/' + task_id).json()['data']['state'], 'cancelled')
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM records')['n'], 0)
        self.assertEqual(self.model.calls['classification'], 1)

    def test_second_round_has_one_shared_quota_and_uses_total_budget(self):
        data, manifest = dataset(notes=8, positions=[])
        for index, ids in enumerate(([1, 2, 3, 4], [5, 6, 7, 8]), 1):
            search_id = 'station-search-' + str(index)
            data['search_runs'].append({'search_id': search_id, 'round_id': 2, 'keyword': '深圳地铁站租房', 'status': 'success'})
            data['search_results'] += [{'search_id': search_id, 'position': p, 'note_id': nid(n), 'status': 'success'} for p, n in enumerate(ids, 1)]
            manifest['search_mappings'][search_id] = {'station_poi': 'station-' + str(index)}
        SourceImporter(self.db).import_data(data, manifest, 'two-stations')
        self.workspace = self.write('/workspace', {})['data']
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': self.workspace['fixed_config']['config_hash']})
        self.refresh()
        self.lock_profile(commute=40, walk=30)
        original = self.maps.station_plan
        def station_plan(*args, **kwargs):
            result = original(*args, **kwargs)
            result['stations'] = [{'poi_id': 'station-1', 'duration_sec': 100}, {'poi_id': 'station-2', 'duration_sec': 200}]
            return result
        self.maps.station_plan = station_plan
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(task['counters']['near_metro']['eligible_count'], 5)
        self.assertEqual(task['counters']['near_metro']['attempt_count'], 5)
        self.assertEqual(self.maps.station_budgets, [2400])

    def test_manual_summary_failures_are_confirmed_independently(self):
        self.prepare()
        self.model.preference_result = 'fail'
        task = self.drain(self.manual()['task_id'])
        for _ in range(2):
            self.assertEqual(task['state'], 'waiting_user', task)
            pending = task['pending_action']
            self.assertTrue(self.db.one('SELECT condition_code FROM conditions WHERE condition_id=?', (pending['condition_id'],))['condition_code'].startswith('pref:'))
            self.write('/conditions/' + pending['condition_id'] + '/decision', {**self.command(pending['condition_version']), 'decision': 'continue'})
            task = self.drain(task['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.model.calls['summary'], 1)
        self.assertEqual(self.record(task)['list_status'], 'candidate')

    def test_comment_limit_is_combined_and_unknown_price_is_not_zero(self):
        data, manifest = dataset(rent=None)
        data['comments'] = [{'note_id': nid(1), 'comment_id': 'comment-' + str(i), 'parent_comment_id': 'comment-0' if i else None, 'body': '测试评论' + str(i)} for i in range(12)]
        SourceImporter(self.db).import_data(data, manifest, 'twelve-comments')
        self.workspace = self.write('/workspace', {})['data']
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': self.workspace['fixed_config']['config_hash']})
        self.refresh()
        self.lock_profile()
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.record(task)['manual_values']['rent_cent'], None)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM comments')['n'], 10)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM comments WHERE level=2')['n'], 9)

    def test_duplicate_submit_and_invalid_auxiliary_facts_never_enter_full_report(self):
        self.prepare()
        task = self.manual()
        self.write('/tasks/import', {**self.command(), 'url': 'https://www.xiaohongshu.com/explore/' + nid(1)}, expected=409)
        self.model.bad_reference = True
        done = self.drain(task['task_id'])
        self.assertEqual(done['state'], 'completed')
        self.assertIn('AI_INVALID_EVIDENCE',self.record(done)['data_completeness']['warnings'])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM reports WHERE kind='full'")['n'], 1)
        self.assertNotIn('not-existing',self.db.one("SELECT content FROM reports WHERE kind='full'")['content'])

    def test_missing_author_history_never_looks_like_confirmed_empty(self):
        self.prepare(author_status='not_attempted')
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(self.record(task)['list_status'], 'candidate')
        self.assertEqual(self.model.calls['author'], 0)
        self.assertEqual(self.model.calls['summary'], 1)
        self.assertTrue(any(x['collection_status']=='not_collected' for x in self.record(task)['data_completeness']['sources']))

    def test_profile_walk_zero_and_greater_than_commute_are_valid(self):
        from rental.schemas import SaveProfile
        body = {'profile_id': 'profile', 'version': 1, 'min_rent_cent': 0, 'max_rent_cent': 400000,
                'max_commute_min': 30, 'preferences': [{'kind': 'agent', 'original_text': '拒绝中介及二房东', 'requirement': 'must'}]}
        for walk in (0, 100):
            self.assertEqual(SaveProfile.model_validate({**body, 'max_walk_min': walk}).max_walk_min, walk)
        from pydantic import ValidationError
        for walk in (-1, 1.5, '', None, True):
            with self.assertRaises(ValidationError):
                SaveProfile.model_validate({**body, 'max_walk_min': walk})

    def test_legacy_database_rejected_without_touching_old_values(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = str(Path(directory) / 'legacy.sqlite')
            connection = sqlite3.connect(filename)
            connection.execute('CREATE TABLE profiles(profile_id TEXT,metro_walk_limit_sec INTEGER)')
            connection.execute("INSERT INTO profiles VALUES('old',600)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                Database(filename)
            check = sqlite3.connect(filename)
            self.assertEqual(check.execute('SELECT metro_walk_limit_sec FROM profiles').fetchone()[0], 600)
            check.close()

    def test_manifest_coverage_missing_is_error_not_zero(self):
        self.prepare()
        batch = self.db.one('SELECT * FROM imports LIMIT 1')
        manifest = decode(batch['source_manifest'])
        for metadata in manifest['searches'].values():
            metadata.pop('discovery_scope_verified', None)
        with self.db.transaction() as connection:
            connection.execute('UPDATE imports SET source_manifest=? WHERE import_id=?', (json.dumps(manifest), batch['import_id']))
        task = self.drain(self.write('/tasks/search', self.command())['data']['task_id'])
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['error']['code'], 'SOURCE_COVERAGE_MISSING')
        self.assertEqual(task['counters']['near_target']['attempt_count'], 0)

    def test_foreign_keys_and_batch_invariants(self):
        self.prepare()
        with self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as connection:
            connection.execute("INSERT INTO records(record_id,profile_id,post_key,created_at) VALUES('bad','unknown',1,'now')")
        self.assertEqual(self.db.one('PRAGMA foreign_key_check'), None)

    def test_reset_response_keeps_same_session_csrf_usable(self):
        self.prepare()
        old = self.command()
        self.workspace = self.write('/profile/reset', {**old, 'old_profile_id': old['profile_id'], 'confirm': True})['data']
        self.assertIn('csrf_token', self.workspace)
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': self.workspace['fixed_config']['config_hash']})


if __name__ == '__main__':
    unittest.main(verbosity=2)
