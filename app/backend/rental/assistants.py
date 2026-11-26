"""Persistent per-record assistants, isolated from investigation and candidate edits."""
import base64
import json
import re
import threading
import time
from .common import DomainError, identifier, now, encode, decode
from .assistant_schemas import CommunicationResult, ContractResult


def validate_tenant_reply(output,payload):
    """Reject clear role/format mistakes; this is not a full semantic verifier."""
    reply=output['reply'].strip()
    if not reply or re.match(r'^(?:建议回复[：:]|你可以这样(?:回复|说)|以下是.{0,12}(?:回复|话术)|作为(?:AI|助手)|我是(?:AI|助手|房东|房屋业主|中介))',reply):
        raise DomainError('AI_INVALID_OUTPUT','建议回复不是可直接发送的租客话术，请重新生成；已有对话保留。',503,True)
    user_statements=payload.get('goal','')+'\n'+'\n'.join(t.get('text','') for t in payload.get('confirmed_turns',[]) if t.get('role')=='self')
    promises=re.findall(r'我(?:(?:现在|马上|今天|明天|这就|会|愿意|同意|承诺|可以|已经|已|先)){0,3}(?:支付(?:定金|押金|租金)?|付款|转账|打款|交定金|付定金|付押金|交押金|给你转|给您转)',reply)
    promises=[text for text in promises if not (text.startswith('我可以') and re.search(re.escape(text)+r'[^。！!；;\n]{0,25}[？?]',reply))]
    claims=re.findall(r'我(?:已经|已)?(?:看过房|核实过产权|核实产权|核实了产权|核实了房东身份)',reply)
    if any(text not in user_statements for text in promises+claims):
        raise DomainError('AI_INVALID_OUTPUT','回复包含你未提供的付款承诺或核实经历，请重新生成；未自动确认或发送。',503,True)
    return output

TABLES='''
CREATE TABLE IF NOT EXISTS assistant_state(record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,kind TEXT NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(record_id,kind));
CREATE TABLE IF NOT EXISTS assistant_files(file_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,file_name TEXT NOT NULL,content BLOB NOT NULL,pages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assistant_jobs(job_id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records ON DELETE CASCADE,kind TEXT NOT NULL,state TEXT NOT NULL,input TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL);
'''

def record_context(record):
    """Keep all business values; route geometry belongs in the frozen report, not prompts."""
    result={**record}
    result['conditions']=[{k:v for k,v in item.items() if k!='route_context'} for item in record.get('conditions',[])]
    return result

def report_context(report):
    if not report:return None
    content={**report.get('content',{})}
    content['conditions']=[{k:v for k,v in item.items() if k!='route_context'} for item in content.get('conditions',[])]
    route=content.get('route')
    if route:
        route_fields=('route_id','mode','duration_sec','total_walk_sec','walking_distance_m','transfer_count','quality','commute_pass','walk_pass','joint_pass')
        segment_fields=('mode','line_name','departure_stop','arrival_stop','duration_sec','distance_m')
        content['route']={k:v for k,v in route.items() if k!='routes'}
        content['route']['routes']=[{**{k:r[k] for k in route_fields if k in r},'segments':[{k:s[k] for k in segment_fields if k in s} for s in r.get('segments',[])]} for r in route.get('routes',[])]
    return {**report,'content':content}

class Assistants:
    def __init__(self,services,model):
        self.svc,self.db,self.model=services,services.db,model
        self.prompt_version='assistants-v4:'+getattr(model,'prompt_version','external')
        self.stop_event=threading.Event();self.thread=None
        self.db.connection.executescript(TABLES)

    def start(self):
        # Unknown in-flight billing is never retried silently after a crash.
        self.db.connection.execute("UPDATE assistant_jobs SET state='failed',error=? WHERE state='running'",(encode({'code':'PROVIDER_OUTCOME_UNKNOWN','message':'重启前调用结果未知，可能已计费，请明确重试。'}),))
        self.thread=threading.Thread(target=self.run,daemon=True,name='rental-assistants');self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.wait(.25):
            self.advance()

    def state(self,owner,rid,kind):
        if kind not in ('communication','contract'):raise DomainError('NOT_FOUND','模块不存在。',404)
        self.svc.owned_record(owner,rid)
        row=self.db.one('SELECT * FROM assistant_state WHERE record_id=? AND kind=?',(rid,kind))
        data=decode(row['data']) if row else {}
        jobs=self.db.all('SELECT job_id,state,error,created_at,input FROM assistant_jobs WHERE record_id=? AND kind=? ORDER BY created_at DESC LIMIT 1',(rid,kind))
        if jobs:
            jobs[0]['phase']=decode(jobs[0].pop('input')).get('processing_phase')
        return {'version':row['version'] if row else 0,'data':data,'job':{**jobs[0],'error':decode(jobs[0]['error']) if jobs[0]['error'] else None} if jobs else None}

    def save(self,c,rid,kind,data,version):
        c.execute('INSERT INTO assistant_state VALUES(?,?,?,?) ON CONFLICT(record_id,kind) DO UPDATE SET version=excluded.version,data=excluded.data',(rid,kind,version+1,encode(data)))

    def command(self,c,owner,rid,kind,request):
        current=self.state(owner,rid,kind);data=current['data'];version=current['version']
        if version!=request.version:raise DomainError('VERSION_CONFLICT','记录已改变，请重新读取。',409)
        if current['job'] and current['job']['state'] in ('queued','running'):
            raise DomainError('ASSISTANT_BUSY','当前任务尚未结束。',409)
        action=request.action
        if kind=='communication':
            if action=='draft':
                for k in ('original_message','goal','style','reply'):data[k]=getattr(request,k)
            elif action=='confirm':
                if not data.get('pending') or not request.reply.strip():raise DomainError('INVALID_STATE','请生成并填写待确认回复。',422)
                data.setdefault('turns',[]).append({'id':identifier('msg'),'role':'self','text':request.reply.strip(),'time':now()})
                data.setdefault('rounds',[])[-1]['final_reply']=request.reply.strip()
                data.update(pending=None,reply='',original_message='',goal='')
            elif action=='revise':
                if not data.get('pending'):raise DomainError('INVALID_STATE','没有待修改回复。',409)
                pending=data.pop('pending');data['turns']=[x for x in data['turns'] if x['id']!=pending['turnId']]
                data.update(original_message=pending['message'],goal=pending['goal'],reply='')
            elif action=='end':
                if data.get('pending') or not data.get('turns'):raise DomainError('INVALID_STATE','请先确认或重新编辑待确认回复。',409)
                data.setdefault('history',[]).append({'session_id':data.get('session_id'),'created_at':now(),'turns':data['turns'],'rounds':data.get('rounds',[])})
                data.update(turns=[],rounds=[],session_id=identifier('session'))
            elif action in ('generate','retry'):
                if data.get('pending'):raise DomainError('INVALID_STATE','请先处理待确认回复。',409)
                if action=='generate':
                    for k in ('original_message','goal','style'):data[k]=getattr(request,k)
                if not data.get('original_message','').strip():raise DomainError('INVALID_REQUEST','对方原话不能为空。',422)
            else:raise DomainError('INVALID_REQUEST','沟通操作不支持。',422)
        else:
            if action not in ('draft','check','retry'):raise DomainError('INVALID_REQUEST','合同操作不支持。',422)
            if action!='retry':data['background']=request.background
            if action in ('check','retry') and not data.get('upload'):raise DomainError('FILE_REQUIRED','请先上传合同。',422)
        self.save(c,rid,kind,data,version)
        if action in ('generate','check','retry'):
            if not self.model.ready():raise DomainError('PROVIDER_NOT_CONFIGURED','尚未配置模型Key，输入可先保存为草稿。',503)
            # Daily per-profile ceiling applies to these independently billed operations too.
            profile=self.svc.profile(owner)
            count=self.db.one('SELECT COUNT(*) AS n FROM assistant_jobs j JOIN records r ON j.record_id=r.record_id WHERE r.profile_id=? AND j.created_at>=?',(profile['profile_id'],now()[:10]))['n']
            if count>=self.svc.settings.task_limit_per_day:raise DomainError('TASK_LIMIT','今日助手任务已达上限。',429)
            snapshot={'draft':data,'record':self.svc.record(owner,rid),'prompt_version':self.prompt_version}
            record=snapshot['record']
            if record.get('full_report_id'):
                snapshot['report']=self.svc.report(owner,record['full_report_id'])
            c.execute('INSERT INTO assistant_jobs VALUES(?,?,?,?,?,?,?)',(identifier('assistant'),rid,kind,'queued',encode(snapshot),None,now()))
        return self.state(owner,rid,kind)

    def upload(self,c,owner,rid,request,raw,pages):
        state=self.state(owner,rid,'contract')
        if state['version']!=request.version:raise DomainError('VERSION_CONFLICT','上传版本冲突，请重读。',409)
        if state['job'] and state['job']['state'] in ('queued','running'):raise DomainError('ASSISTANT_BUSY','检查进行中，请稍后上传。',409)
        profile=self.svc.profile(owner)
        total=self.db.one('SELECT COALESCE(SUM(length(content)),0) AS n FROM assistant_files f JOIN records r ON f.record_id=r.record_id WHERE r.profile_id=?',(profile['profile_id'],))['n']
        if total+len(raw)>100*1024*1024:raise DomainError('STORAGE_LIMIT','当前画像合同文件总量上限100MB。',413)
        filename='合同图片组.zip' if request.additional_images else request.file_name
        fid=identifier('file');c.execute('INSERT INTO assistant_files VALUES(?,?,?,?,?)',(fid,rid,filename,raw,encode(pages)))
        data=state['data'];data['upload']={'file_id':fid,'file_name':filename,'page_count':len(pages)}
        self.save(c,rid,'contract',data,state['version']);return self.state(owner,rid,'contract')

    def file(self,owner,rid,fid):
        self.svc.owned_record(owner,rid)
        row=self.db.one('SELECT * FROM assistant_files WHERE file_id=? AND record_id=?',(fid,rid))
        if not row:raise DomainError('NOT_FOUND','文件不存在。',404)
        return {'file_id':fid,'file_name':row['file_name'],'content_base64':base64.b64encode(row['content']).decode(),'pages':[{'page':p['page'],'text':p.get('text',''),'image':p.get('image')} for p in decode(row['pages'])]}

    def advance(self):
        job=self.db.one("SELECT * FROM assistant_jobs WHERE state='queued' ORDER BY created_at LIMIT 1")
        if not job:return False
        with self.db.transaction() as c:
            if not c.execute("UPDATE assistant_jobs SET state='running' WHERE job_id=? AND state='queued'",(job['job_id'],)).rowcount:return False
        try:
            snapshot=decode(job['input']);draft=snapshot['draft']
            if snapshot['prompt_version']!=self.prompt_version:raise DomainError('PROMPT_VERSION_UNAVAILABLE','模型提示版本已变化，请重新提交。',409)
            if job['kind']=='communication':
                self.processing_phase(job,'正在生成租客回复')
                payload={k:v for k,v in snapshot.items() if k!='draft'}
                payload['record']=record_context(snapshot['record'])
                if snapshot.get('report'):payload['report']=report_context(snapshot['report'])
                payload.update(message=draft['original_message'],goal=draft.get('goal',''),style=draft.get('style','柔和'),confirmed_turns=draft.get('turns',[]))
                payload['conversation_roles']={'speaker':'租客本人','recipient':'当前房东／对方','self':'租客已经确认的回复','other':'对方已经说过的原话',
                    'current_message':'本轮对方原话，租客尚未回复','reply_format':'仅可直接复制发送的租客话术',
                    'private_analysis_fields':['recognized_topic','send_checks','signals','remaining_questions'],'unverified_claims':'原话与历史往来都不自动成为已核实事实'}
                result=self.model.invoke('communication',payload)
                output=validate_tenant_reply(CommunicationResult.model_validate(result['payload']).model_dump(),payload)
            else:
                file=self.db.one('SELECT * FROM assistant_files WHERE file_id=?',(draft['upload']['file_id'],))
                if not file:return True # Reset has already invalidated this job.
                pages=decode(file['pages']);texts=[]
                for p in pages:
                    text=p.get('text','')
                    if p.get('image'):
                        self.processing_phase(job,'正在识别合同页面文字')
                        ocr=self.model.invoke('contract_ocr',{'page':p['page'],'_images':[p['image']]})['payload']
                        if not ocr['readable'] or not ocr['text'].strip():raise DomainError('OCR_UNREADABLE','存在无法完整识别的页面，请提供清晰版本。',422)
                        text=ocr['text']
                    texts.append('位置 '+str(p['page'])+'\n'+text)
                text='\n\n'.join(texts)
                if len(text)>80000:raise DomainError('DOCUMENT_LENGTH_LIMIT','识别文字超过80000字符，未截断。',422)
                self.processing_phase(job,'正在进行AI合同检查')
                result=self.model.invoke('contract',{'document':text,'background':draft.get('background',''),'record':record_context(snapshot['record']),'report':report_context(snapshot.get('report'))})
                output=ContractResult.model_validate(result['payload']).model_dump()
                for clause in output['clause_notices']:
                    if clause['kind']!='missing' and (not clause['quote'].strip() or clause['quote'] not in text):raise DomainError('AI_INVALID_OUTPUT','条款引用未在合同识别原文找到。',503)
                if any(value and value not in text for value in output['sources'].values()):raise DomainError('AI_INVALID_OUTPUT','摘要引用不属于合同原文。',503)
                output['risk_count']=len(output['clause_notices'])
                output['disclaimer']='AI辅助理解，不构成法律意见，不保证合同合法、有效或不存在风险。'
            with self.db.transaction() as c:
                fresh=self.db.one('SELECT state FROM assistant_jobs WHERE job_id=?',(job['job_id'],))
                if not fresh or fresh['state']!='running':return True
                state=self.db.one('SELECT * FROM assistant_state WHERE record_id=? AND kind=?',(job['record_id'],job['kind']))
                data=decode(state['data'])
                if job['kind']=='communication':
                    mid=identifier('msg');data.setdefault('session_id',identifier('session'))
                    data.setdefault('turns',[]).append({'id':mid,'role':'other','text':draft['original_message'],'time':now()})
                    data.update(output);data['generated_style']=draft.get('style','柔和');data['pending']={'turnId':mid,'message':draft['original_message'],'goal':draft.get('goal','')}
                    data.setdefault('rounds',[]).append({'generated_at':now(),'input':draft['original_message'],'goal':draft.get('goal',''),'style':draft.get('style','柔和'),'generated':output,'final_reply':None,'model_metadata':{k:v for k,v in result.items() if k!='payload'}})
                else:
                    data.setdefault('scans',[]).append({'scan_id':identifier('scan'),'created_at':now(),'status':output['status'],'file_name':file['file_name'],'file_id':file['file_id'],'result':output,'background':draft.get('background',''),'snapshot':snapshot['record'],'extracted_text':text,'model_metadata':{k:v for k,v in result.items() if k!='payload'}})
                self.save(c,job['record_id'],job['kind'],data,state['version'])
                c.execute("UPDATE assistant_jobs SET state='succeeded',error=NULL WHERE job_id=?",(job['job_id'],))
        except Exception as error:
            safe={'code':error.code,'message':error.message} if isinstance(error,DomainError) else {'code':'ASSISTANT_FAILED','message':'处理失败，原始资料和既有结果保留。'}
            with self.db.transaction() as c:c.execute("UPDATE assistant_jobs SET state='failed',error=? WHERE job_id=?",(encode(safe),job['job_id']))
        return True

    def processing_phase(self,job,phase):
        with self.db.transaction() as c:
            current=self.db.one('SELECT input FROM assistant_jobs WHERE job_id=?',(job['job_id'],))
            if current:
                payload=decode(current['input']);payload['processing_phase']=phase
                c.execute('UPDATE assistant_jobs SET input=? WHERE job_id=?',(encode(payload),job['job_id']))
