import unittest,json
from . import test_system as base
from rental.common import DomainError,decode,encode
class TaskExperienceTest(unittest.TestCase):
    setUp=base.SystemTest.setUp
    tearDown=base.SystemTest.tearDown
    write=base.SystemTest.write
    prepare=base.SystemTest.prepare
    refresh=base.SystemTest.refresh
    lock_profile=base.SystemTest.lock_profile
    command=base.SystemTest.command
    drain=base.SystemTest.drain
    def legacy_search(self):
        task=self.write('/tasks/search',self.command())['data'];row=self.db.one('SELECT progress FROM tasks WHERE task_id=?',(task['task_id'],));p=decode(row['progress']);p['config_snapshot'].pop('auxiliary_policy',None)
        self.db.connection.execute('UPDATE tasks SET progress=? WHERE task_id=?',(encode(p),task['task_id']))
        return self.drain(task['task_id'])
    def test_missing_community_waits_without_business_failure_and_can_be_completed(self):
        self.prepare(community=None)
        task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        self.assertEqual(task['state'],'waiting_user');self.assertTrue(task['can_skip']);self.assertEqual(task['current_step'],'community_prompt')
        self.assertFalse(self.db.one("SELECT condition_id FROM conditions WHERE condition_code='community' AND result='fail'"))
        post=task['location_resolution']['post'];self.assertEqual(post['note_id'],base.nid(1));self.assertIn('community_name',post['body'])
        self.write('/tasks/'+task['task_id']+'/location',{**self.command(task['version']),'community_name':'测试小区'})
        done=self.drain(task['task_id']);self.assertEqual(done['state'],'completed');self.assertEqual(done['counters']['near_target']['candidate_added_count'],1)
        self.assertEqual(self.model.calls['body'],1)
    def test_missing_community_can_skip_without_passing_condition(self):
        self.prepare(community=None);task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        skipped=self.write('/tasks/'+task['task_id']+'/skip',self.command(task['version']))['data'];done=self.drain(task['task_id'])
        self.assertEqual(done['counters']['near_target']['skipped_count'],1);self.assertEqual(done['counters']['near_target']['attempt_count'],1)
        self.assertEqual(done['counters']['near_target']['candidate_added_count'],0)
    def test_technical_source_failure_is_not_a_business_rejection(self):
        self.prepare(author_status='not_attempted');task=self.legacy_search()
        self.assertEqual(task['state'],'completed');self.assertEqual(task['result']['summary']['technical_failure'],1);self.assertEqual(task['result']['summary']['screened'],0)
        rid=task['result']['record_ids'][0];record=self.client.get('/api/v1/records/'+rid).json()['data'];self.assertEqual(record['disposition'],'technical_failure')
        self.assertEqual(self.model.calls['summary'],0)
        self.assertEqual(next(s for s in task['stage_progress'] if s['stage']=='author')['state'],'technical_failure')
        resumed=self.write('/records/'+rid+'/resume',self.command(record['version']))['data'];again=self.drain(resumed['task_id'])
        self.assertEqual(again['result']['summary']['candidate_added'],1)
        self.assertEqual(self.model.calls['author'],0)
        transition=decode(self.db.one('SELECT progress FROM tasks WHERE task_id=?',(resumed['task_id'],))['progress'])['policy_transition']
        self.assertEqual(transition['to'],'available_evidence_v1')
    def test_latest_modern_task_preferred_over_legacy_and_new_search_is_modern(self):
        self.prepare();old=self.write('/tasks/search',self.command())['data']
        row=self.db.one('SELECT progress FROM tasks WHERE task_id=?',(old['task_id'],));p=decode(row['progress']);p['config_snapshot'].pop('station_selection_strategy');p['current_step']='station_plan';p['round']=2
        p['last_error']={'code':'STATION_CATALOG_REQUIRED','message':'old'}
        self.db.connection.execute("UPDATE tasks SET state='failed',progress=? WHERE task_id=?",(encode(p),old['task_id']))
        new=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        workspace=self.client.get('/api/v1/workspace').json()['data']
        self.assertEqual(workspace['current_task_id'],new['task_id']);self.assertNotIn(old['task_id'],[t['task_id'] for t in workspace['active_tasks']])
        legacy=next(t for t in workspace['recent_tasks'] if t['task_id']==old['task_id']);self.assertTrue(legacy['legacy_policy']);self.assertEqual(legacy['error']['code'],'LEGACY_DISCOVERY_POLICY')
        self.assertEqual(new['config_snapshot']['station_selection_strategy'],'manual_station_times_v1')

    def test_source_failure_has_persisted_phase_and_each_post_has_accurate_stages(self):
        self.prepare(notes=2,author_status='not_attempted')
        task=self.legacy_search()
        self.assertEqual(len(task['record_results']),2)
        for item in task['record_results']:
            self.assertEqual(item['outcome'],'technical_failure')
            self.assertEqual(item['stopped_step'],'author');self.assertEqual(item['failure_phase'],'source_read')
            self.assertEqual(set(item['completed_steps']),{'classification','body','map','comments'})
            states={s['stage']:s for s in item['stage_progress']}
            self.assertEqual(states['basics']['state'],'succeeded');self.assertEqual(states['author']['state'],'technical_failure')
            self.assertEqual(states['author']['steps']['author'],'failed');self.assertEqual(states['community']['state'],'blocked')
            step=self.db.one("SELECT attempt_log FROM steps WHERE record_id=? AND step_name='author'",(item['record_id'],))
            self.assertFalse(decode(step['attempt_log'])[-1]['model_called'])
        self.assertEqual(self.model.calls['author'],0)

    def test_legacy_source_failure_without_step_is_projected_and_resume_reuses_success(self):
        self.prepare(author_status='not_attempted')
        task=self.legacy_search();rid=task['record_results'][0]['record_id']
        self.db.connection.execute("DELETE FROM steps WHERE record_id=? AND step_name='author'",(rid,))
        task=self.client.get('/api/v1/tasks/'+task['task_id']).json()['data']
        self.assertEqual(task['record_results'][0]['stage_progress'][2]['steps']['author'],'failed')
        before=self.model.calls.copy();maps=self.maps.calls.copy()
        # Simulates an explicitly repaired source in this isolated in-memory fixture.
        self.db.connection.execute("UPDATE authors SET quality=?",(encode({'status':'success'}),))
        record=self.client.get('/api/v1/records/'+rid).json()['data']
        resumed=self.write('/records/'+rid+'/resume',self.command(record['version']))['data']
        done=self.drain(resumed['task_id']);self.assertEqual(done['result']['summary']['candidate_added'],1)
        historical=self.client.get('/api/v1/tasks/'+task['task_id']).json()['data']
        self.assertEqual(historical['result']['summary']['technical_failure'],1)
        self.assertEqual(historical['result']['summary']['recovered_candidates'],1)
        self.assertEqual(historical['record_results'][0]['recovery']['state'],'completed')
        for stage in ('classification','body','comments'):self.assertEqual(self.model.calls[stage],before[stage])
        self.assertEqual(self.maps.calls,maps);self.assertEqual(self.model.calls['author'],1)
        self.assertEqual(self.model.calls['community'],0);self.assertEqual(self.model.calls['summary'],1)

    def test_skipped_map_failure_is_skipped_in_last_view_and_per_post_results(self):
        self.prepare()
        def unresolved(*args,**kwargs):raise DomainError('LOCATION_UNCONFIRMED','需要确认实际地址',503)
        self.maps.evaluate=unresolved
        task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        self.write('/tasks/'+task['task_id']+'/skip',self.command(task['version']))
        done=self.drain(task['task_id'])
        self.assertEqual(done['record_results'][0]['outcome'],'skipped')
        self.assertEqual(done['stage_progress'][1]['state'],'skipped')
        self.assertEqual(done['stage_progress'][2]['state'],'blocked')
        self.assertEqual(done['result']['summary']['skipped'],1)
        self.assertEqual(done['result']['summary']['technical_failure'],0)

    def test_explicit_author_revision_preserves_frozen_batch_and_resumes_without_rebilling(self):
        from rental.source_repair import repair_authors
        from rental.common import now
        self.prepare(author_status='not_attempted')
        task=self.legacy_search();rid=task['record_results'][0]['record_id']
        profile_id=task['profile_id'];old=task['config_snapshot']['import_id']
        reports=[{'source':'live_author_profile','fetched_at':now(),'data':{'author_id':base.AUTHOR,'quality':'success','nickname':'实际作者','profile_text':'实际简介',
            'history_titles':[{'note_id':base.nid(100),'title':'真实返回标题','original_url':'https://www.xiaohongshu.com/explore/'+base.nid(100)}]}}]
        original=self.db.all('SELECT * FROM authors WHERE import_id=?',(old,));calls=self.model.calls.copy();maps=self.maps.calls.copy()
        plan=repair_authors(self.db,profile_id,reports,'test-repair');self.assertFalse(plan['applied']);self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM imports')['n'],1)
        revision=repair_authors(self.db,profile_id,reports,'test-repair',apply=True)
        self.assertEqual(self.db.all('SELECT * FROM authors WHERE import_id=?',(old,)),original)
        self.assertEqual(len(self.app.state.services.source.search(revision['to_import_id'],'near_target',target=task['config_snapshot']['target'])),1)
        record=self.client.get('/api/v1/records/'+rid).json()['data']
        resumed=self.write('/records/'+rid+'/resume',self.command(record['version']))['data']
        self.assertEqual(resumed['config_snapshot']['import_id'],revision['to_import_id'])
        self.assertEqual(resumed['config_snapshot']['planning_context'],task['config_snapshot']['planning_context'])
        done=self.drain(resumed['task_id']);self.assertEqual(done['result']['summary']['candidate_added'],1)
        for stage in ('classification','body','comments'):self.assertEqual(self.model.calls[stage],calls[stage])
        self.assertEqual(self.maps.calls,maps);self.assertEqual(self.model.calls['author'],1)
        self.assertFalse(self.db.all('PRAGMA foreign_key_check'))

    def test_author_revision_rejects_wrong_author_and_unconfirmed_empty_success(self):
        from rental.source_repair import repair_authors
        from rental.common import now
        self.prepare(author_status='not_attempted');task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        for aid,titles in ((base.AUTHOR,[]),(base.nid(123),[{'note_id':base.nid(100),'title':'标题','original_url':'https://www.xiaohongshu.com/explore/'+base.nid(100)}])):
            report={'source':'live_author_profile','fetched_at':now(),'data':{'author_id':aid,'quality':'success','history_titles':titles}}
            with self.assertRaises(DomainError):repair_authors(self.db,task['profile_id'],[report],'bad-repair',apply=True)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM imports')['n'],1)
    def test_inclusive_budget_and_unknown_information_are_not_early_rejections(self):
        self.prepare(notes=3)
        for i,price in ((1,200000),(2,400000),(3,None)):
            self.db.connection.execute('UPDATE posts SET body=? WHERE note_id=?',(json.dumps({'city':None,'rent_cent':price,'community_name':'测试小区'}),base.nid(i)))
        task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        self.assertEqual(task['state'],'completed');self.assertEqual(task['counters']['near_target']['candidate_added_count'],3)
        self.assertFalse(self.db.one("SELECT condition_id FROM conditions WHERE result='fail'"));self.assertEqual(self.model.calls['summary'],3)
    def test_commute_and_walking_must_be_met_by_same_route(self):
        self.prepare();self.maps.routes=[(2401,0),(1200,601)]
        task=self.drain(self.write('/tasks/search',self.command())['data']['task_id']);self.assertEqual(task['counters']['near_target']['candidate_added_count'],0)
        self.assertEqual(task['result']['summary']['screened'],1);self.assertEqual(self.model.calls['author'],0)
    def test_exact_route_limits_and_preference_only_decided_at_summary(self):
        self.prepare();self.maps.routes=[(2400,600)];self.model.preference_result='fail'
        task=self.drain(self.write('/tasks/search',self.command())['data']['task_id'])
        for stage in ('comments','author','summary'):self.assertGreater(self.model.calls[stage],0)
        self.assertEqual(self.model.calls['preference'],0)
        self.assertEqual(task['result']['summary']['screened'],1)
        self.assertFalse(self.db.one("SELECT condition_id FROM conditions WHERE condition_code IN('commute','walk') AND result='fail'"))
