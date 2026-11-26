"""Additional normal and failure branches exercised through the actual API and worker."""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from starlette.testclient import TestClient
from rental.common import DomainError, decode, encode
from rental.providers import ModelGateway
from rental.source import SourceImporter
from . import test_system as base


class AcceptanceBranches(TestCase):
    setUp = base.SystemTest.setUp
    tearDown = base.SystemTest.tearDown
    write = base.SystemTest.write
    prepare = base.SystemTest.prepare
    refresh = base.SystemTest.refresh
    command = base.SystemTest.command
    lock_profile = base.SystemTest.lock_profile
    manual = base.SystemTest.manual
    drain = base.SystemTest.drain
    record = base.SystemTest.record

    def test_summary_policy_is_sent_and_unknown_must_does_not_block_flow(self):
        """偏好规则传给综合模型，must未知仍可完成候选流程，不强凑符合。"""
        self.prepare()
        original = self.model.invoke
        captured = []
        def invoke(stage, payload):
            if stage == 'summary': captured.append(copy.deepcopy(payload))
            return original(stage, payload)
        self.model.invoke = invoke
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'], 'completed')
        self.assertEqual(self.record(task)['list_status'], 'candidate')
        self.assertEqual(len(captured), 1)
        self.assertIn('发布者自述', captured[0]['preference_policy']['unknown'])
        self.assertEqual(self.model.calls['classification'], 1)
        self.assertEqual(self.model.calls['body'], 1)

    def test_manual_abort_then_resume_keeps_body_and_stops_downstream(self):
        """手工遇到超预算可中止、写简报，再从历史续接并逐项确认。"""
        self.prepare(rent=500000)
        task = self.drain(self.manual()['task_id'])
        pending = task['pending_action']
        self.write('/conditions/'+pending['condition_id']+'/decision',
                   {**self.command(pending['condition_version']), 'decision': 'abort'})
        task = self.client.get('/api/v1/tasks/'+task['task_id']).json()['data']
        self.assertEqual(task['result']['outcome'], 'screened')
        record = self.record(task)
        self.assertEqual(self.model.calls['comments'], 0)
        self.assertEqual(record['list_status'], 'screened')
        resumed = self.write('/records/'+record['record_id']+'/resume', self.command(record['version']))['data']
        task = self.drain(resumed['task_id'])
        pending = task['pending_action']
        self.write('/conditions/'+pending['condition_id']+'/decision',
                   {**self.command(pending['condition_version']), 'decision': 'continue'})
        self.assertEqual(self.drain(task['task_id'])['state'], 'completed')
        self.assertEqual(self.model.calls['body'], 1)
        self.assertEqual(self.model.calls['classification'], 1)

    def test_cancel_at_waiting_community_then_resume_and_supplement(self):
        """补小区时取消可从历史恢复，不能重复做成功分类。"""
        self.prepare(community=None)
        task = self.drain(self.manual()['task_id'])
        self.write('/tasks/'+task['task_id']+'/cancel', {**self.command(task['version']), 'confirm': True})
        record = self.record(task)
        self.assertEqual(record['list_status'], 'screened')
        resumed = self.write('/records/'+record['record_id']+'/resume', self.command(record['version']))['data']
        task = self.drain(resumed['task_id'])
        record = self.record(task)
        self.write('/records/'+record['record_id']+'/community', {**self.command(record['version']), 'name': '测试小区'})
        self.assertEqual(self.drain(task['task_id'])['state'], 'completed')
        self.assertEqual(self.model.calls['classification'], 1)

    def test_completed_task_cannot_cancel_retry_or_resume_candidate(self):
        """终态任务不能取消重试，候选不能作为筛除记录续接。"""
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        self.write('/tasks/'+task['task_id']+'/cancel', {**self.command(task['version']), 'confirm': True}, expected=409)
        self.write('/tasks/'+task['task_id']+'/retry', self.command(task['version']), expected=409)
        record = self.record(task)
        self.write('/records/'+record['record_id']+'/resume', self.command(record['version']), expected=409)
        self.write('/records/'+record['record_id']+'/restore', self.command(record['version']), expected=409)

    def test_idempotency_conflict_and_missing_key_have_no_partial_writes(self):
        """幂等键换请求体及缺失幂等键均失败且不新增任务。"""
        self.prepare(notes=2)
        payload = {**self.command(), 'url': 'https://www.xiaohongshu.com/explore/'+base.nid(1)}
        self.write('/tasks/import', payload, key='same')
        payload['url'] = 'https://www.xiaohongshu.com/explore/'+base.nid(2)
        result = self.write('/tasks/import', payload, key='same', expected=409)
        self.assertEqual(result['error']['code'], 'IDEMPOTENCY_CONFLICT')
        response = self.client.post('/api/v1/tasks/import', json=payload, headers={
            'Origin': 'http://testserver', 'X-CSRF-Token': self.workspace['csrf_token']})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM tasks')['n'], 1)

    def test_invalid_json_oversize_extra_fields_and_csrf_reject_before_write(self):
        """损坏JSON、超大请求、未知字段和跨站写入不会生成任务。"""
        self.prepare()
        headers = {'Origin': 'http://testserver', 'X-CSRF-Token': self.workspace['csrf_token'], 'Idempotency-Key': 'bad'}
        for content, status in ((b'{', 422), (b'x'*256001, 413)):
            with self.subTest(status=status):
                self.assertEqual(self.client.post('/api/v1/tasks/search', content=content, headers=headers).status_code, status)
        self.write('/tasks/search', {**self.command(), 'unrecognized': True}, expected=422)
        for origin, csrf in (('https://external.invalid', self.workspace['csrf_token']), ('http://testserver', 'wrong')):
            with self.subTest(origin=origin, csrf_valid=csrf == self.workspace['csrf_token']):
                response = self.client.post('/api/v1/tasks/search', json=self.command(),
                    headers={**headers, 'Origin': origin, 'X-CSRF-Token': csrf})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM tasks')['n'], 0)

    def test_expired_session_can_reinitialize_with_new_owner_and_csrf(self):
        """会话过期后拒绝旧请求，新初始化有独立画像和有效CSRF。"""
        self.prepare()
        old = self.command()['profile_id']
        expired = (datetime.now(timezone.utc)-timedelta(days=self.settings.session_days+1)).isoformat()
        with self.db.transaction() as connection:
            connection.execute('UPDATE profiles SET created_at=?', (expired,))
        self.assertEqual(self.client.get('/api/v1/workspace').status_code, 401)
        self.workspace = self.write('/workspace', {})['data']
        self.assertNotEqual(self.command()['profile_id'], old)
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': self.workspace['fixed_config']['config_hash']})

    def test_task_and_request_rate_limits_return_429_without_extra_task(self):
        """任务日额度和HTTP频率限制都阻止新增业务操作。"""
        self.prepare(notes=2)
        self.settings.task_limit_per_day = 1
        self.manual()
        self.write('/tasks/import', {**self.command(), 'url': 'https://www.xiaohongshu.com/explore/'+base.nid(2)}, expected=429)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM tasks')['n'], 1)
        self.settings.requests_per_minute = 1
        self.assertEqual(self.client.get('/api/v1/workspace').status_code, 429)

    def test_cross_owner_cannot_read_task_report_evidence_or_history_records(self):
        """其他匿名用户无法读取任务、报告、证据或房源。"""
        self.prepare()
        task = self.drain(self.manual()['task_id'])
        record = self.record(task)
        evidence = self.db.one('SELECT evidence_id FROM evidence')['evidence_id']
        with TestClient(self.app) as other:
            response = other.post('/api/v1/workspace', json={}, headers={'Origin': 'http://testserver', 'Idempotency-Key': 'other'})
            self.assertEqual(response.status_code, 201)
            for path in ('/tasks/'+task['task_id'], '/records/'+record['record_id'],
                         '/reports/'+record['full_report_id'], '/evidence/'+evidence,
                         '/records/'+record['record_id']+'/viewing'):
                with self.subTest(path=path):
                    self.assertEqual(other.get('/api/v1'+path).status_code, 404)

    def test_pagination_is_stable_and_invalid_cursors_do_not_leak_data(self):
        """分页不重复漏项，错误游标及数量返回明确校验错误。"""
        self.prepare(notes=3)
        for note in (1, 2, 3):
            self.drain(self.manual(note)['task_id'])
        seen, cursor = [], None
        for _ in range(3):
            page = self.client.get('/api/v1/records', params={'limit': 1, **({'cursor': cursor} if cursor else {})}).json()['data']
            seen += [row['record_id'] for row in page['items']]
            cursor = page['next_cursor']
        self.assertEqual(len(set(seen)), 3)
        self.assertIsNone(cursor)
        for query in ('limit=0', 'limit=101', 'limit=abc', 'cursor=not-a-valid-cursor'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/v1/records?'+query).status_code, 422)

    def test_duplicate_archive_and_stale_viewing_are_atomic(self):
        """重复批量归档及过期看房保存不能部分覆盖旧记录。"""
        self.prepare()
        record = self.record(self.drain(self.manual()['task_id']))
        item = {'record_id': record['record_id'], 'version': record['version']}
        self.write('/records/archive', {**self.command(), 'items': [item, item], 'confirm': True}, expected=422)
        self.assertEqual(self.client.get('/api/v1/records/'+record['record_id']).json()['data']['list_status'], 'candidate')
        viewing = self.client.get('/api/v1/records/'+record['record_id']+'/viewing').json()['data']
        payload = {**self.command(0), 'conclusion': 'hold', 'summary': 'first', 'followup': '',
            'general_checks': viewing['general_checks'], 'risk_checks': viewing['risk_checks'],
            'template_version': viewing['template']['template_version']}
        self.write('/records/'+record['record_id']+'/viewing', payload, method='PUT')
        payload['summary'] = 'must not overwrite'
        self.write('/records/'+record['record_id']+'/viewing', payload, method='PUT', expected=409)
        self.assertEqual(self.db.one('SELECT summary FROM viewing')['summary'], 'first')

    def test_prompt_snapshot_matches_runtime_and_old_prompt_stops_without_call(self):
        """任务冻结实际提示版本，版本丢失不静默换规则或付费。"""
        self.prepare()
        task = self.manual()
        progress = decode(self.db.one('SELECT progress FROM tasks')['progress'])
        self.assertEqual(progress['config_snapshot']['prompt_version'], ModelGateway.prompt_version)
        progress['config_snapshot']['prompt_version'] = 'unavailable-old-prompt'
        with self.db.transaction() as connection:
            connection.execute('UPDATE tasks SET progress=?', (encode(progress),))
        stopped = self.drain(task['task_id'])
        self.assertEqual(stopped['error']['code'], 'PROMPT_VERSION_UNAVAILABLE')
        self.assertEqual(sum(self.model.calls.values()), 0)

    def test_summary_missing_or_duplicate_preference_is_rejected(self):
        """综合判断遗漏或重复偏好时，不能生成完整报告。"""
        self.prepare(notes=3)
        original = self.model.invoke
        mode = 'missing'
        def malformed(stage, payload):
            output = original(stage, payload)
            if stage == 'summary':
                current = output['payload']['preferences']
                output['payload']['preferences'] = current[:1] if mode == 'missing' else [current[0]]*2 if mode == 'duplicate' else []
            return output
        self.model.invoke = malformed
        for number, mode in enumerate(('missing', 'duplicate', 'empty'), 1):
            with self.subTest(mode=mode):
                task = self.drain(self.manual(number)['task_id'])
                self.assertEqual(task['state'], 'failed')
                self.assertEqual(task['error']['code'], 'AI_INVALID_OUTPUT')
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM reports WHERE kind='full'")['n'], 0)

    def test_wrong_target_and_missing_source_preserve_draft(self):
        """目标hash不符及未导源库不能锁定画像，补导源后可以正常保存。"""
        self.workspace = self.write('/workspace', {})['data']
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': 'wrong'}, expected=409)
        self.assertIsNone(self.refresh()['target_confirmed_at'])
        self.write('/profile/confirm-target', {**self.command(), 'config_hash': self.workspace['fixed_config']['config_hash']})
        self.refresh()
        payload = {**self.command(), 'min_rent_cent': 200000, 'max_rent_cent': 400000,
            'max_commute_min': 40, 'max_walk_min': 10, 'preferences': [
                {'kind': 'agent', 'original_text': '拒绝中介及二房东', 'requirement': 'must'}]}
        self.write('/profile/save', payload, expected=503)
        self.assertEqual(self.refresh()['state'], 'draft')
        data, manifest = base.dataset()
        SourceImporter(self.db).import_data(data, manifest, 'later-source')
        self.write('/profile/save', payload)
        self.assertEqual(self.refresh()['state'], 'locked')

    def test_viewing_wrong_template_missing_extra_and_nonboolean_checks_are_atomic(self):
        """错误模板、漏项、多项或非布尔勾选均不会产生部分现场记录。"""
        self.prepare()
        record = self.record(self.drain(self.manual()['task_id']))
        viewing = self.client.get('/api/v1/records/'+record['record_id']+'/viewing').json()['data']
        original = {**self.command(0), 'conclusion': 'hold', 'summary': 'must not save', 'followup': '',
            'general_checks': viewing['general_checks'], 'risk_checks': viewing['risk_checks'],
            'template_version': viewing['template']['template_version']}
        for mode in ('template', 'missing', 'extra', 'nonboolean'):
            payload = copy.deepcopy(original)
            if mode == 'template': payload['template_version'] = 'wrong-template'
            elif mode == 'missing': payload['risk_checks'].pop(next(iter(payload['risk_checks'])))
            elif mode == 'extra': payload['risk_checks']['invented'] = True
            else: payload['general_checks'][next(iter(payload['general_checks']))] = 'true'
            with self.subTest(mode=mode):
                self.write('/records/'+record['record_id']+'/viewing', payload, method='PUT', expected=422)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM viewing')['n'], 0)

    def test_definite_preference_without_evidence_is_rejected(self):
        """模型凭空给出确定符合或不符结论时拒收。"""
        self.prepare()
        original = self.model.invoke
        def malformed(stage, payload):
            output = original(stage, payload)
            if stage == 'summary':
                output['payload']['preferences'][0].update(result='pass', evidence_ids=[])
            return output
        self.model.invoke = malformed
        task = self.drain(self.manual()['task_id'])
        self.assertEqual(task['error']['code'], 'AI_INVALID_OUTPUT')
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM reports WHERE kind='full'")['n'], 0)

    def test_source_version_conflict_and_corruption_leave_frozen_batch_unchanged(self):
        """重复版本换内容、损坏源关联均不覆盖已冻结资料。"""
        data, manifest = self.prepare()
        changed = copy.deepcopy(data)
        changed['notes'][0]['body'] = 'different'
        with self.assertRaises(DomainError) as conflict:
            SourceImporter(self.db).import_data(changed, manifest, 'test-source')
        self.assertEqual(conflict.exception.code, 'IMPORT_VERSION_CONFLICT')
        for mutation in ('duplicate_note', 'orphan_comment', 'duplicate_position'):
            broken = copy.deepcopy(data)
            if mutation == 'duplicate_note': broken['notes'].append(broken['notes'][0])
            elif mutation == 'orphan_comment': broken['comments'][0]['note_id'] = base.nid(999)
            else: broken['search_results'].append(broken['search_results'][0])
            with self.subTest(mutation=mutation):
                with self.assertRaises(DomainError):
                    SourceImporter(self.db).import_data(broken, manifest, 'broken-source')
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM imports')['n'], 1)

    def test_unprocessed_source_wrong_types_are_reported_not_python_crashes(self):
        """未处理原始数据的标识和计数类型错误返回校验结果，不抛Python内部错误。"""
        data, manifest = base.dataset()
        cases = []
        for collection, field, value in (('notes','note_id',[]), ('search_runs','search_id',{}),
                ('search_runs','returned_count','5'), ('search_results','position',[]),
                ('comments','comment_id',{}), ('author_history_runs','author_id',[])):
            broken = copy.deepcopy(data); broken[collection][0][field] = value; cases.append(broken)
        for index, broken in enumerate(cases):
            with self.subTest(case=index):
                self.assertFalse(SourceImporter.inspect(broken)['valid'])
                with self.assertRaises(DomainError):
                    SourceImporter(self.db).import_data(broken, manifest, 'invalid')
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM imports')['n'], 0)

    def test_invalid_source_manifest_is_rejected_without_import(self):
        """映射不是对象时拒绝导入，不留下半个冻结批次。"""
        data, _ = base.dataset()
        for manifest in (None, [], {'coverage': []}, {'search_mappings': 'invalid'}, {'communities': []}):
            with self.subTest(manifest=manifest):
                with self.assertRaises(DomainError) as error:
                    SourceImporter(self.db).import_data(data, manifest, 'invalid')
                self.assertEqual(error.exception.code, 'SOURCE_VALIDATION_FAILED')
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM imports')['n'], 0)

    def test_frozen_report_ignores_manual_numeric_changes(self):
        """费用、人工通勤、备注的全部清空或修改不重算报告。"""
        self.prepare()
        record = self.record(self.drain(self.manual()['task_id']))
        before = self.client.get('/api/v1/reports/'+record['full_report_id']).json()['data']
        calls = copy.deepcopy(self.model.calls)
        for group, values in (('cost', {'rent_cent': None, 'monthly_cent': None, 'initial_cent': None}),
                              ('commute', {'commute_sec': 999}), ('remark', {'remark': ''})):
            record = self.write('/records/'+record['record_id'], {**self.command(record['version']), 'group': group, **values}, method='PATCH')['data']
        self.assertEqual(self.client.get('/api/v1/reports/'+record['full_report_id']).json()['data'], before)
        self.assertEqual(self.model.calls, calls)
