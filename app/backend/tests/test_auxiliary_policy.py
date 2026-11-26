import copy,json,sqlite3,unittest
from unittest.mock import Mock
from rental.common import DomainError,decode,encode,identifier
from . import test_system as base


class AuxiliaryPolicyTest(unittest.TestCase):
    setUp=base.SystemTest.setUp
    tearDown=base.SystemTest.tearDown
    write=base.SystemTest.write
    prepare=base.SystemTest.prepare
    refresh=base.SystemTest.refresh
    lock_profile=base.SystemTest.lock_profile
    command=base.SystemTest.command
    drain=base.SystemTest.drain
    manual=base.SystemTest.manual
    record=base.SystemTest.record

    def absent(self):
        self.prepare(author_status='not_attempted',comments_status='not_collected')
        self.db.connection.execute("UPDATE authors SET nickname=NULL,profile_text=NULL,history_titles='[]'")
        self.db.connection.execute('DELETE FROM comments')

    def capture(self,change=None):
        original=self.model.invoke;self.inputs={}
        def invoke(stage,payload):
            self.inputs.setdefault(stage,[]).append(copy.deepcopy(payload));result=original(stage,payload)
            if stage=='summary' and change:change(result['payload'],payload)
            return result
        self.model.invoke=invoke

    def full(self,task):return decode(self.db.one("SELECT content FROM reports WHERE record_id=? AND kind='full'",(task['record_id'] or task['result']['record_ids'][0],))['content'])

    def test_all_auxiliary_missing_still_reports_unknown_without_fake_empty_or_extra_models(self):
        self.absent();self.capture();task=self.drain(self.manual()['task_id']);content=self.full(task)
        self.assertEqual(self.record(task)['list_status'],'candidate');self.assertFalse(task['pending_action'])
        self.assertTrue(all(p['result']=='unknown' for p in content['preferences']))
        self.assertTrue(all(p['non_blocking_reason']=='no_explicit_violation_with_insufficient_evidence' for p in content['preferences']))
        self.assertEqual(sum(self.model.calls[s] for s in ('comments','author','community','preference')),0)
        statuses={s['source_kind']:s['collection_status'] for s in content['data_completeness']['sources']}
        self.assertEqual(statuses['author'],'not_collected');self.assertEqual(statuses['original_comments'],'not_collected')
        self.assertEqual(statuses['community'],'not_collected');self.assertEqual(statuses['preference'],'skipped_optional')
        self.assertEqual(self.inputs['summary'][0]['auxiliary_policy'],'available_evidence_v1')
        self.assertTrue(self.inputs['summary'][0]['evidence']);self.assertIsNone(task['error'])

    def test_partial_author_and_failed_comment_sources_keep_available_raw_evidence(self):
        self.prepare(author_status='partial',comments_status='failed');self.capture()
        task=self.drain(self.manual()['task_id']);content=self.full(task)
        kinds={e['locator']['type'] for e in self.inputs['summary'][0]['evidence']}
        self.assertTrue({'body','comment','author_profile','history_title'}<=kinds)
        self.assertTrue(any(s['collection_status']=='failed' for s in content['data_completeness']['sources']))
        self.assertEqual(self.record(task)['list_status'],'candidate')

    def test_confirmed_empty_comments_remain_distinct_from_uncollected(self):
        self.absent();row=self.db.one('SELECT quality FROM posts');q=decode(row['quality']);q['primary_comments']='empty_confirmed';q['replies']='empty_confirmed'
        self.db.connection.execute('UPDATE posts SET quality=?',(encode(q),));task=self.drain(self.manual()['task_id'])
        sources=self.full(task)['data_completeness']['sources']
        self.assertTrue(any(s['source_kind']=='original_comments' and s['collection_status']=='empty_confirmed' for s in sources))
        self.assertTrue(any(s['source_kind']=='author' and s['collection_status']=='not_collected' for s in sources))

    def test_auxiliary_read_error_is_visible_and_does_not_stop_summary(self):
        self.absent();self.app.state.services.source.author=Mock(side_effect=DomainError('SOURCE_UNAVAILABLE','作者读取失败',503))
        task=self.drain(self.manual()['task_id']);self.assertEqual(self.record(task)['list_status'],'candidate')
        self.assertIn('SOURCE_UNAVAILABLE',self.full(task)['data_completeness']['warnings']);self.assertEqual(self.model.calls['summary'],1)

    def test_malformed_auxiliary_source_is_degraded_without_hiding_database_errors(self):
        self.absent();self.db.connection.execute("UPDATE authors SET history_titles='invalid-json'")
        task=self.drain(self.manual()['task_id']);self.assertEqual(self.record(task)['list_status'],'candidate')
        self.assertIn('AUXILIARY_SOURCE_INVALID',self.full(task)['data_completeness']['warnings'])

    def test_missing_original_body_is_still_a_core_failure(self):
        self.absent();self.db.connection.execute('UPDATE posts SET body=NULL')
        task=self.drain(self.manual()['task_id'])
        self.assertFalse(self.db.one("SELECT report_id FROM reports WHERE kind='full'"));self.assertEqual(self.model.calls['summary'],0)

    def test_budget_exhaustion_is_not_swallowed_by_optional_model_degradation(self):
        self.prepare();self.settings.model_call_limit=2
        task=self.drain(self.manual()['task_id']);self.assertEqual(task['state'],'failed');self.assertEqual(task['error']['code'],'MODEL_BUDGET_LIMIT')
        self.assertFalse(self.db.one("SELECT report_id FROM reports WHERE kind='full'"))

    def counterexample(self,mode='auto',flexible=False):
        self.absent();self.db.connection.execute("UPDATE posts SET title='当前房间长期存在明确噪音反证'")
        if flexible:self.db.connection.execute("UPDATE preferences SET requirement='flexible' WHERE kind='custom'")
        def contrary(result,payload):
            refs=[e['evidence_id'] for e in payload['evidence'] if '明确噪音反证' in e['excerpt']]
            self.assertTrue(refs)
            for p in result['preferences']:
                if next(x for x in payload['preferences'] if x['preference_id']==p['preference_id'])['kind']=='custom':p.update(result='fail',reason='已有明确反证',evidence_ids=refs)
        self.capture(contrary)
        task=self.write('/tasks/search',self.command())['data'] if mode=='auto' else self.manual()
        return self.drain(task['task_id'])

    def test_existing_body_counterevidence_screens_must_only_at_summary_despite_missing_auxiliary(self):
        task=self.counterexample();self.assertEqual(self.record(task)['list_status'],'screened')
        self.assertEqual(task['result']['summary']['screened'],1);self.assertEqual(self.model.calls['summary'],1)
        self.assertEqual(self.model.calls['preference'],0)
        self.assertIsNotNone(decode(self.db.one("SELECT content FROM reports WHERE kind='brief'")['content'])['data_completeness'])

    def test_manual_counterevidence_requires_explicit_choice(self):
        task=self.counterexample(mode='manual');self.assertEqual(task['state'],'waiting_user')
        pending=task['pending_action'];self.assertEqual(pending['kind'],'condition')
        result=self.write('/conditions/'+pending['condition_id']+'/decision',{**self.command(pending['condition_version']),'decision':'continue'})['data']['task']
        done=self.drain(result['task_id']);self.assertEqual(self.record(done)['list_status'],'candidate')
        self.assertTrue(any(c['result']=='fail' and c['decision']=='continue' for c in self.record(done)['conditions']))

    def test_flexible_counterevidence_is_visible_and_does_not_screen(self):
        task=self.counterexample(flexible=True);self.assertEqual(self.record(task)['list_status'],'candidate')
        self.assertIn('fail',[p['result'] for p in self.full(task)['preferences']]);self.assertFalse(task['pending_action'])

    def test_two_preferences_share_existing_community_evidence_without_special_search(self):
        self.prepare(notes=2,positions=[base.nid(1)])
        self.db.connection.execute("UPDATE posts SET title='测试小区居住实测安静采光足' WHERE note_id=?",(base.nid(2),))
        self.db.connection.execute("INSERT INTO preferences VALUES(?,?,?,?,?,?)",(identifier('pref'),self.command()['profile_id'],'custom','采光','must',2))
        original=self.app.state.services.source.auxiliary
        self.app.state.services.source.auxiliary=Mock(wraps=original)
        def support(result,payload):
            refs=[e['evidence_id'] for e in payload['evidence'] if '居住实测安静采光足' in e['excerpt']]
            self.assertTrue(refs)
            for p in result['preferences']:
                if next(x for x in payload['preferences'] if x['preference_id']==p['preference_id'])['kind']=='custom':p.update(result='pass',reason='现有小区资料支持',evidence_ids=refs)
        self.capture(support);task=self.drain(self.manual()['task_id'])
        self.assertEqual(self.record(task)['list_status'],'candidate');self.assertEqual(self.model.calls['preference'],0)
        self.assertTrue(all(len(c.args)==2 for c in self.app.state.services.source.auxiliary.call_args_list))
        self.assertEqual(sum(p['result']=='pass' for p in self.full(task)['preferences']),2)

    def test_optional_model_failures_degrade_but_raw_sources_reach_summary(self):
        self.prepare();self.model.fail['author']=3;self.capture();task=self.drain(self.manual()['task_id'])
        self.assertEqual(self.record(task)['list_status'],'candidate')
        self.assertIn('MODEL_NETWORK_ERROR',self.full(task)['data_completeness']['warnings'])
        self.assertIn('author_profile',{e['locator']['type'] for e in self.inputs['summary'][0]['evidence']})
        self.assertEqual(next(s for s in task['stage_progress'] if s['stage']=='author')['state'],'degraded')

    def test_retry_summary_reuses_optional_failure_degradation_without_rebilling(self):
        self.prepare();self.model.fail['author']=3;self.model.fail['summary']=3
        task=self.drain(self.manual()['task_id']);self.assertEqual(task['state'],'failed');calls=self.model.calls.copy()
        self.write('/tasks/'+task['task_id']+'/retry',self.command(task['version']));done=self.drain(task['task_id'])
        self.assertEqual(self.record(done)['list_status'],'candidate');self.assertEqual(self.model.calls['author'],calls['author'])
        self.assertEqual(self.model.calls['body'],calls['body']);self.assertEqual(self.model.calls['summary'],4)

    def test_invalid_core_summary_reference_still_blocks_success(self):
        self.absent()
        def invalid(result,payload):result['preferences'][0].update(result='fail',evidence_ids=['foreign-evidence'])
        self.capture(invalid);task=self.drain(self.manual()['task_id'])
        self.assertEqual(task['state'],'failed');self.assertEqual(task['error']['code'],'AI_INVALID_EVIDENCE')
        self.assertFalse(self.db.one("SELECT report_id FROM reports WHERE kind='full'"))

    def test_shared_database_fault_is_not_swallowed_as_missing_auxiliary(self):
        self.prepare();self.app.state.services.source.author=Mock(side_effect=sqlite3.OperationalError('unavailable'))
        task=self.drain(self.manual()['task_id']);self.assertEqual(task['state'],'failed');self.assertEqual(task['error']['code'],'DATABASE_ERROR')
        self.assertFalse(self.db.one("SELECT report_id FROM reports WHERE kind='full'"))

    def test_full_report_is_immutable_when_more_auxiliary_data_arrives(self):
        self.absent();task=self.drain(self.manual()['task_id']);row=self.db.one("SELECT report_id,content FROM reports WHERE kind='full'");calls=self.model.calls.copy()
        self.db.connection.execute("UPDATE authors SET profile_text='新增资料',quality=?",(encode({'status':'success'}),))
        self.client.get('/api/v1/reports/'+row['report_id']);self.client.get('/api/v1/records')
        self.assertEqual(self.db.one('SELECT content FROM reports WHERE report_id=?',(row['report_id'],))['content'],row['content']);self.assertEqual(self.model.calls,calls)
