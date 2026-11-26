import base64
import io
import unittest
from unittest.mock import patch
from .test_system import SystemTest
from rental.common import DomainError
from rental.documents import decode_document

class AssistantTest(unittest.TestCase):
    setUp=SystemTest.setUp
    tearDown=SystemTest.tearDown
    write=SystemTest.write
    prepare=SystemTest.prepare
    refresh=SystemTest.refresh
    lock_profile=SystemTest.lock_profile
    command=SystemTest.command
    manual=SystemTest.manual
    drain=SystemTest.drain
    record=SystemTest.record

    def begin(self):
        self.prepare();self.rid=self.record(self.drain(self.manual()['task_id']))['record_id']
        self.base='/records/'+self.rid+'/assistants/'
        self.assistant=self.app.state.assistants
        self.prompts=[];original=self.model.invoke
        def invoke(stage,payload):
            self.prompts.append((stage,payload))
            if stage=='communication':return {'payload':{'reply':'请提供押金退还约定。','recognized_topic':'押金','send_checks':['核实条款'],'signals':'尚待核实','remaining_questions':['何时退还？']},'usage':{'total_tokens':10}}
            if stage=='contract_ocr':return {'payload':{'readable':True,'text':'月租3000元，押金3000元。'}}
            if stage=='contract':return {'payload':{'status':'需协商','summary':'请确认退押金条件','recognized_summary':{'monthly_rent_yuan':'3000'},'sources':{'monthly_rent_yuan':'月租3000元'},'pre_signing_questions':['何时退押金？'],'clause_notices':[{'title':'退押金','quote':'','location':'全文','kind':'missing','reason':'未见明确条件','question':'如何退还？'}],'disclaimer':'辅助理解'}}
            return original(stage,payload)
        self.model.invoke=invoke

    def get(self,kind):return self.client.get('/api/v1'+self.base+kind).json()['data']
    def op(self,kind,action,**kw):return self.write(self.base+kind,{'version':self.get(kind)['version'],'action':action,**kw})['data']

    def test_multiturn_confirm_revise_history_and_context(self):
        self.begin();self.op('communication','draft',original_message='押金怎么退？')
        self.op('communication','generate',original_message='押金怎么退？');self.assistant.advance()
        state=self.get('communication');self.assertEqual(len(state['data']['turns']),1)
        self.op('communication','confirm',reply='请写入合同。')
        self.op('communication','generate',original_message='可以写');self.assistant.advance()
        self.assertEqual(len(self.prompts[-1][1]['confirmed_turns']),2)
        self.op('communication','revise');self.assertEqual(len(self.get('communication')['data']['turns']),2)
        self.op('communication','generate',original_message='重新提问');self.assistant.advance()
        self.op('communication','confirm',reply='谢谢。');self.op('communication','end')
        data=self.get('communication')['data'];self.assertEqual(len(data['history'][0]['turns']),4)
        self.assertEqual(data['history'][0]['rounds'][0]['final_reply'],'请写入合同。')
        self.assertEqual(data['turns'],[])
        self.assertEqual(self.client.get('/api/v1/records/'+self.rid).json()['data']['manual_values']['rent_cent'],300000)

    def docx(self,text='月租3000元，押金3000元。'):
        from docx import Document
        doc=Document();doc.add_paragraph(text);buf=io.BytesIO();doc.save(buf);return base64.b64encode(buf.getvalue()).decode()

    def upload(self,name='合同.docx',encoded=None):
        return self.write(self.base+'contract/upload',{'version':self.get('contract')['version'],'file_name':name,'content_base64':encoded or self.docx()})['data']

    def test_contract_versions_quotes_and_original_file(self):
        self.begin();first=self.upload()['data']['upload'];self.op('contract','check',background='口头另约');self.assistant.advance()
        state=self.get('contract');self.assertEqual(state['job']['state'],'succeeded',state)
        scan=state['data']['scans'][0];self.assertEqual(scan['file_id'],first['file_id'])
        self.upload(encoded=self.docx('月租4000元'))
        response=self.client.get('/api/v1'+self.base+'contract/files/'+scan['file_id'])
        self.assertEqual(response.status_code,200)
        self.assertIn('月租3000元',response.json()['data']['pages'][0]['text'])
        self.assertEqual(len(self.get('contract')['data']['scans']),1)

    def test_failures_isolation_reset_and_late_result(self):
        self.begin()
        self.write(self.base+'communication',{'version':0,'action':'generate'},expected=422)
        self.op('communication','draft',original_message='保留原话')
        self.op('communication','generate',original_message='保留原话')
        with patch.object(self.model,'invoke',side_effect=DomainError('MODEL_NETWORK_ERROR','网络中断',503)):
            self.assistant.advance()
        state=self.get('communication');self.assertEqual(state['job']['state'],'failed');self.assertEqual(state['data']['original_message'],'保留原话')
        self.op('communication','retry');self.assistant.advance();self.assertIsNotNone(self.get('communication')['data']['pending'])
        self.write(self.base+'communication',{'version':0,'action':'draft'},expected=409)
        self.assertEqual(self.client.get('/api/v1'+self.base.replace(self.rid,'foreign')+'communication').status_code,404)
        profile=self.refresh();self.write('/profile/reset',{**self.command(),'old_profile_id':profile['profile_id'],'confirm':True})
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM assistant_state')['n'],0)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM assistant_jobs')['n'],0)

    def test_file_validation_and_image_ocr(self):
        self.begin()
        for name,content in [('bad.exe',base64.b64encode(b'bad').decode()),('bad.pdf',base64.b64encode(b'bad').decode())]:
            self.write(self.base+'contract/upload',{'version':0,'file_name':name,'content_base64':content},expected=422)
        from PIL import Image
        image=Image.new('RGB',(100,100),'white');buf=io.BytesIO();image.save(buf,format='PNG')
        self.upload('扫描.png',base64.b64encode(buf.getvalue()).decode());self.op('contract','check');self.assistant.advance()
        self.assertEqual(self.get('contract')['job']['state'],'succeeded')
        self.assertTrue(any(s=='contract_ocr' for s,p in self.prompts))

    def test_fabricated_quote_does_not_create_success(self):
        self.begin();self.upload();self.op('contract','check')
        original=self.model.invoke
        def bad(stage,payload):
            result=original(stage,payload)
            if stage=='contract':result['payload']['sources']={'rent':'不存在的合同原文'}
            return result
        self.model.invoke=bad;self.assistant.advance()
        state=self.get('contract');self.assertEqual(state['job']['state'],'failed');self.assertFalse(state['data'].get('scans'))

    def test_reset_during_ai_call_cannot_write_late_result(self):
        self.begin();self.op('communication','generate',original_message='你好')
        original=self.model.invoke
        def reset(stage,payload):
            result=original(stage,payload)
            profile=self.refresh();self.write('/profile/reset',{**self.command(),'old_profile_id':profile['profile_id'],'confirm':True})
            return result
        self.model.invoke=reset;self.assistant.advance()
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM assistant_state')['n'],0)

    def test_grouped_images_page_order_and_durable_storage(self):
        self.begin()
        from PIL import Image
        buf=io.BytesIO();Image.new('RGB',(20,20),'white').save(buf,format='PNG');encoded=base64.b64encode(buf.getvalue()).decode()
        data=self.write(self.base+'contract/upload',{'version':0,'file_name':'1.png','content_base64':encoded,'additional_images':[{'file_name':'2.png','content_base64':encoded}]})['data']
        self.assertEqual(data['data']['upload']['page_count'],2)
        row=self.db.one('SELECT * FROM assistant_files');self.assertEqual(row['file_name'],'合同图片组.zip')
        import sqlite3,tempfile
        from pathlib import Path
        from rental.db import Database
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'persistent.sqlite');destination=sqlite3.connect(path);self.db.connection.backup(destination);destination.close()
            reopened=Database(path)
            self.assertEqual(reopened.one('SELECT file_id FROM assistant_files')['file_id'],row['file_id']);reopened.close()

    def test_context_projection_keeps_facts_waivers_and_stops_without_geometry(self):
        from rental.assistants import record_context,report_context
        condition={'condition_code':'walk','result':'fail','decision':'continue','route_context':{'polyline':'x'*120000}}
        record={'remark':'原始备注','manual_values':{'rent_cent':300000},'conditions':[condition]}
        report={'content':{'facts':[{'text':'原文事实'}],'risks':[{'title':'待核实'}],'conditions':[condition],
            'route':{'selected_route_id':'r1','reference_date':'2026-11-20','routes':[{'route_id':'r1','mode':'bus','duration_sec':1700,'total_walk_sec':1020,'walk_pass':False,'segments':[{'mode':'bus','line_name':'M123','departure_stop':{'name':'起点'},'arrival_stop':{'name':'终点'},'steps':[{'polyline':'x'*120000}]}]}]}}}
        result=report_context(report)
        self.assertEqual(result['content']['facts'],report['content']['facts'])
        self.assertEqual(result['content']['risks'],report['content']['risks'])
        self.assertEqual(result['content']['conditions'][0]['decision'],'continue')
        self.assertEqual(result['content']['route']['routes'][0]['segments'][0]['line_name'],'M123')
        self.assertFalse(result['content']['route']['routes'][0]['walk_pass'])
        self.assertLess(len(str(result)),2000)
        self.assertEqual(record_context(record)['manual_values'],record['manual_values'])
        self.assertIn('route_context',record['conditions'][0])

del SystemTest
