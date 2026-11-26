import json,unittest,io
from unittest.mock import patch
from rental.config import Settings
from rental.providers import ModelGateway
from rental.assistants import validate_tenant_reply
from rental.common import DomainError
from . import test_system as base
from .test_assistants import AssistantTest


class PresentationFeedbackTest(unittest.TestCase):
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

    def test_summary_wire_requests_thematic_synthesis_without_changing_business_version(self):
        sent=[]
        output={'summary':'现有资料无法确认全部条件。','facts':[],'preferences':[],'conflicts':[],'missing':[],'risks':[],
                'report_topics':[{'section':'housing','topic':'费用','conclusion':'费用资料不足，无法确认。','evidence_ids':[],'status':'unknown'}]}
        def respond(request,timeout):
            sent.append(json.loads(request.data));return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(output,ensure_ascii=False)}}]}).encode())
        with patch('urllib.request.urlopen',respond):result=ModelGateway(Settings(model_key='isolated-secret')).invoke('summary',{'evidence':[],'preferences':[]})
        prompt=sent[0]['messages'][0]['content']
        for term in ('文献综述','report_topics','双方观点','无法确认','不得按帖子','preference_id'):self.assertIn(term,prompt)
        self.assertEqual(result['report_presentation_version'],'synthesis-v1')
        self.assertEqual(result['prompt_version'],'evidence-v1.37-5')
        self.assertEqual(result['payload']['report_topics'][0]['status'],'unknown')

    def test_report_projection_uses_final_synthesis_not_raw_investigation_transcripts(self):
        from rental.presentation import report_sections
        content={'summary':'现有信息显示费用尚未完整披露，实际居住情况仍需核实。',
            'investigations':[{'stage':'community','result':{'facts':[{'text':'不要逐条展示的评论原文','evidence_ids':['e1']}]}}],
            'facts':[{'text':'也不逐条展示的中间事实','evidence_ids':['e1']}],
            'conflicts':[{'text':'装修时间存在不同说法','evidence_ids':['e1','e2']}]}
        original=json.dumps(content,ensure_ascii=False)
        sections=report_sections(content)
        self.assertEqual(sections['housing'][0]['conclusion'],content['summary'])
        self.assertNotIn('不要逐条',json.dumps(sections,ensure_ascii=False))
        self.assertNotIn('中间事实',json.dumps(sections,ensure_ascii=False))
        self.assertEqual(sections['housing'][1]['evidence_ids'],['e1','e2'])
        self.assertIn('无法确认',sections['housing'][1]['conclusion'])
        self.assertEqual(json.dumps(content,ensure_ascii=False),original)

    def test_thematic_report_persists_in_same_summary_call_and_read_is_free(self):
        self.prepare();invoke=self.model.invoke
        def model(stage,payload):
            result=invoke(stage,payload)
            if stage=='summary':
                ids=[x['evidence_id'] for x in payload['evidence']]
                result['payload']['report_topics']=[{'section':'housing','topic':'费用安排',
                    'conclusion':'现有资料仅披露部分费用，首期成本仍需核实。','evidence_ids':ids[:1],
                    'status':'unknown','disagreements':[],'uncertainty':'需确认押金和其他费用。'},
                    {'section':'housing','topic':'居住体验','conclusion':'对居住体验存在分歧，尚不能确定实际情况。',
                     'evidence_ids':ids[:2],'status':'disputed','disagreements':[
                         {'text':'部分线索描述居住较安静','evidence_ids':ids[:1]},
                         {'text':'另有线索提示噪声问题','evidence_ids':ids[1:2]}],
                     'uncertainty':'核实是否为同一楼栋与时段。'}]
            return result
        self.model.invoke=model
        task=self.drain(self.manual()['task_id']);self.assertEqual(task['state'],'completed')
        record=self.record(task);calls=self.model.calls.copy(),self.maps.calls.copy()
        frozen=self.db.one('SELECT content FROM reports WHERE report_id=?',(record['full_report_id'],))['content']
        report=self.client.get('/api/v1/reports/'+record['full_report_id']).json()['data']
        topics=report['report_sections']['housing'];self.assertEqual(len(topics),2)
        self.assertIn('无法确认',topics[0]['conclusion']);self.assertEqual(len(topics[1]['disagreements']),2)
        self.assertEqual(self.model.calls['summary'],1)
        self.assertEqual((self.model.calls,self.maps.calls),calls)
        self.assertEqual(self.db.one('SELECT content FROM reports WHERE report_id=?',(record['full_report_id'],))['content'],frozen)

    def test_invalid_theme_evidence_and_one_sided_conflict_are_rejected(self):
        from rental.schemas import ReportTopic
        from rental.engine import Engine
        topic={'section':'housing','topic':'环境','conclusion':'资料不足，无法确认。','status':'unknown','evidence_ids':['foreign']}
        with self.assertRaises(DomainError):Engine._validate_evidence(ReportTopic.model_validate(topic).model_dump(),{'owned'})
        self.prepare();invoke=self.model.invoke
        def model(stage,payload):
            result=invoke(stage,payload)
            if stage=='summary':
                ids=[x['evidence_id'] for x in payload['evidence']]
                result['payload']['report_topics']=[{**topic,'status':'disputed','evidence_ids':ids[:1],
                    'disagreements':[{'text':'只给一方说法','evidence_ids':ids[:1]}]}]
            return result
        self.model.invoke=model
        task=self.drain(self.manual()['task_id']);self.assertEqual(task['state'],'failed')
        self.assertEqual(task['error']['code'],'AI_INVALID_OUTPUT')
        self.assertEqual(self.db.one("SELECT count(*) AS n FROM reports WHERE kind='full'")['n'],0)

    def test_stage_handled_is_distinct_from_source_completeness_and_waiting(self):
        self.prepare();svc=self.app.state.services
        task={'profile_id':self.workspace['profile']['profile_id'],'state':'completed'}
        states={name:'succeeded' for name in ('classification','body','map','comments','author','community','preference','summary')}
        for quality,state in [('partial','partial'),('not_collected','not_collected'),('failed','degraded'),('skipped_optional','skipped_optional')]:
            stages=svc.stage_progress(task,{'collection_states':{'author':quality}},states,{})
            author=next(x for x in stages if x['stage']=='author')
            self.assertEqual(author['state'],state);self.assertTrue(author['handled'])
        waiting=svc.stage_progress({**task,'state':'waiting_user'},{'current_step':'author'},states,{})
        self.assertFalse(next(x for x in waiting if x['stage']=='author')['handled'])
        failed=svc.stage_progress({**task,'state':'failed'},{'current_step':'author'},states,{'message':'测试技术失败'})
        self.assertFalse(next(x for x in failed if x['stage']=='author')['handled'])
        with self.db.transaction() as c:c.execute("DELETE FROM preferences WHERE profile_id=? AND kind='custom'",(task['profile_id'],))
        stages=svc.stage_progress(task,{},states,{})
        preference=next(x for x in stages if x['stage']=='preference')
        self.assertEqual(preference['state'],'not_required');self.assertTrue(preference['handled'])

    def test_no_custom_preference_stays_pending_until_cursor_reaches_fifth_stage(self):
        self.prepare();profile_id=self.workspace['profile']['profile_id']
        with self.db.transaction() as c:c.execute("DELETE FROM preferences WHERE profile_id=? AND kind='custom'",(profile_id,))
        svc=self.app.state.services;task={'profile_id':profile_id,'state':'running'}
        before={s:'succeeded' for s in ('classification','body','map','comments','author','community')}
        for current in ('classification','body','map','comments','author','community','community_prompt'):
            stage=next(s for s in svc.stage_progress(task,{'current_step':current},before,{}) if s['stage']=='preference')
            self.assertEqual(stage['state'],'pending',current);self.assertFalse(stage['handled'])
        for current in ('preference','summary'):
            stage=next(s for s in svc.stage_progress(task,{'current_step':current},before,{}) if s['stage']=='preference')
            self.assertEqual(stage['state'],'not_required');self.assertTrue(stage['handled'])
        for state in ('failed','waiting_user','cancelled'):
            stage=next(s for s in svc.stage_progress({**task,'state':state},{'current_step':'community'},{**before,'community':'failed'},{}) if s['stage']=='preference')
            self.assertEqual(stage['state'],'pending');self.assertFalse(stage['handled'])

    def test_no_custom_preference_real_pipeline_only_turns_green_at_summary(self):
        self.prepare();profile_id=self.workspace['profile']['profile_id']
        with self.db.transaction() as c:c.execute("DELETE FROM preferences WHERE profile_id=? AND kind='custom'",(profile_id,))
        task=self.manual();invoke=self.model.invoke;observed=[]
        def model(stage,payload):
            if stage in ('body','comments','author','community','summary'):
                snapshot=self.client.get('/api/v1/tasks/'+task['task_id']).json()['data']
                optional=next(x for x in snapshot['stage_progress'] if x['stage']=='preference')
                observed.append((stage,optional['state'],optional['handled']))
            return invoke(stage,payload)
        self.model.invoke=model;done=self.drain(task['task_id'])
        self.assertEqual(done['state'],'completed')
        self.assertTrue(any(stage=='body' for stage,_,_ in observed))
        for stage,status,handled in observed:
            self.assertEqual(status,'not_required' if stage=='summary' else 'pending');self.assertEqual(handled,stage=='summary')
        self.assertEqual(next(x for x in done['stage_progress'] if x['stage']=='preference')['state'],'not_required')
        self.assertEqual(self.model.calls['preference'],0);self.assertEqual(self.model.calls['summary'],1)

    def test_quality_summary_groups_duplicates_without_internal_details(self):
        from rental.presentation import quality_summary
        raw={'sources':[{'source_kind':'community_post','collection_status':'failed','source_ref':'a'*24,'reason':'读取失败：'+'a'*24}]*3+[{'source_kind':'author','collection_status':'complete'}]}
        result=quality_summary(raw,['读取失败：'+'a'*24,'押金未约定','押金未约定'])
        self.assertEqual(len(result['limited']),1);self.assertEqual(result['notes'],['押金未约定'])
        self.assertNotIn('a'*24,json.dumps(result));self.assertNotIn('source_ref',json.dumps(result))

    def test_evidence_text_descriptors_match_report_viewing_and_owned_ids_without_calls(self):
        self.prepare();record=self.record(self.drain(self.manual()['task_id']));before=self.model.calls.copy(),self.maps.calls.copy()
        report=self.client.get('/api/v1/reports/'+record['full_report_id']).json()['data']
        viewing=self.client.get('/api/v1/records/'+record['record_id']+'/viewing').json()['data']
        catalog=report['evidence_sources'];self.assertEqual(catalog,viewing['evidence_sources'])
        self.assertEqual(set(catalog),set(report['content']['evidence_ids']))
        self.assertEqual(sorted(x['number'] for x in catalog.values()),list(range(1,len(catalog)+1)))
        self.assertTrue(any('原帖正文' in x['label'] for x in catalog.values()))
        self.assertTrue(any('作者历史标题' in x['label'] for x in catalog.values()))
        self.assertEqual(self.app.state.services.evidence_sources(record['record_id'],['not-owned']),{})
        self.assertEqual((self.model.calls,self.maps.calls),before)

    def test_communication_wire_prompt_separates_tenant_reply_from_private_analysis(self):
        sent=[];output={'reply':'我想先看看定金退还约定，方便发我确认吗？','recognized_topic':'定金','send_checks':['核对收款主体'],'signals':'身份待核实','remaining_questions':['退还条件是否书面约定？']}
        def respond(request,timeout):
            sent.append(json.loads(request.data));return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(output,ensure_ascii=False)}}]}).encode())
        with patch('urllib.request.urlopen',respond):
            result=ModelGateway(Settings(model_key='isolated-secret')).invoke('communication',{'message':'先交定金再看房','goal':'','confirmed_turns':[{'role':'self','text':'我想先看合同'},{'role':'other','text':'先付定金'}]})
        prompt=sent[0]['messages'][0]['content'];self.assertIn('租客第一人称',prompt);self.assertIn('send_checks',prompt);self.assertIn('other是对方原话',prompt);self.assertIn('不擅自承诺付款',prompt)
        self.assertNotIn('Classify using the complete title and body',prompt)
        self.assertEqual(result['payload']['reply'],output['reply'])

    def test_reply_guard_rejects_clear_analysis_role_swap_and_unprovided_promises(self):
        for reply in ('   ','建议回复：你应该先核实房东','我是房东，可以给你优惠','我马上支付定金。','我已经看过房，没问题。'):
            with self.subTest(reply=reply),self.assertRaises(DomainError):validate_tenant_reply({'reply':reply},{'goal':'','confirmed_turns':[{'role':'other','text':reply}]})
        for reply in ('我想先看看合同，方便发我吗？','我不会先付定金，请先明确退还条件。','我可以转账支付吗？'):
            self.assertEqual(validate_tenant_reply({'reply':reply},{'goal':'','confirmed_turns':[]})['reply'],reply)


class CommunicationRolesTest(AssistantTest):
    def test_processing_phase_is_persisted_while_actual_model_is_running(self):
        self.begin();original=self.model.invoke;observed=[]
        def invoke(stage,payload):
            if stage=='communication':observed.append(self.get('communication')['job']['phase'])
            return original(stage,payload)
        self.model.invoke=invoke;self.op('communication','generate',original_message='请发合同');self.assistant.advance()
        self.assertEqual(observed,['正在生成租客回复']);self.assertEqual(self.get('communication')['job']['state'],'succeeded')
    # Only the new method is added to the explicit acceptance suite.
    def test_multiturn_tenant_roles_and_bad_generation_keep_confirmed_history(self):
        self.begin();self.op('communication','generate',original_message='先付定金');self.assistant.advance()
        self.op('communication','confirm',reply='我想先看合同。')
        self.op('communication','generate',original_message='你先付款，我才发合同');self.assistant.advance()
        payload=self.prompts[-1][1];self.assertEqual(payload['conversation_roles']['speaker'],'租客本人')
        self.assertEqual(payload['confirmed_turns'][0]['role'],'other');self.assertEqual(payload['confirmed_turns'][1]['role'],'self')
        self.assertEqual(payload['confirmed_turns'][1]['text'],'我想先看合同。');self.assertEqual(payload['message'],'你先付款，我才发合同')
        self.op('communication','revise');before=self.get('communication')['data']['turns']
        self.model.invoke=lambda stage,payload:{'payload':{'reply':'我马上支付定金。','recognized_topic':'定金','send_checks':[],'signals':'','remaining_questions':[]}}
        self.op('communication','generate',original_message='先付款');self.assistant.advance();state=self.get('communication')
        self.assertEqual(state['job']['state'],'failed');self.assertEqual(state['job']['error']['code'],'AI_INVALID_OUTPUT')
        self.assertEqual(state['data']['turns'],before);self.assertFalse(state['data'].get('pending'))
