import base64
from datetime import datetime, timedelta, timezone

from .checklist import GENERAL_ITEMS, TEMPLATE_VERSION
from .common import DomainError, decode, digest, encode, identifier, now
from .source import SourceRepository, parse_link
from .presentation import quality_summary, report_sections

GLOBAL_TASK_ERRORS={'MODEL_BUDGET_LIMIT','MAP_BUDGET_LIMIT','PROVIDER_NOT_CONFIGURED','PLANNING_DATE_REQUIRED',
                    'UNSUPPORTED_PLANNING_CONTEXT','PROMPT_VERSION_UNAVAILABLE','MODEL_HTTP_401','MODEL_HTTP_402','MODEL_HTTP_403',
                    'AMAP_10001','AMAP_10002','AMAP_10003','AMAP_10005','AMAP_10012','LEGACY_DISCOVERY_POLICY'}

class Services:
    def __init__(self, database, settings, prompt_version):
        self.db, self.settings = database, settings
        self.prompt_version = prompt_version
        self.source = SourceRepository(database)
        from .station_table import StationTableRepository
        self.station_tables = StationTableRepository(settings.station_table, settings.fixed_config())

    @staticmethod
    def version(row, expected):
        if row['version'] != expected:
            raise DomainError('VERSION_CONFLICT', '数据已更新，请重新读取后保存。', current_version=row['version'])

    def profile(self, owner, profile_id=None):
        row = self.db.one('SELECT * FROM profiles WHERE owner_hash=?', (owner,))
        if not row:
            raise DomainError('SESSION_REQUIRED', '请先初始化工作区。', 401)
        if profile_id and row['profile_id'] != profile_id:
            raise DomainError('PROFILE_CHANGED', '画像已更换，旧请求不能写入。')
        if datetime.fromisoformat(row['created_at']) + timedelta(days=self.settings.session_days) < datetime.now(timezone.utc):
            raise DomainError('SESSION_EXPIRED', '匿名工作区保留期限已到，请重新初始化。', 401)
        return row

    def profile_public(self, row):
        if not row:
            return None
        preferences = self.db.all('SELECT preference_id,kind,original_text,requirement,display_order FROM preferences WHERE profile_id=? ORDER BY display_order', (row['profile_id'],))
        return {'schema_version': 1, 'profile_id': row['profile_id'], 'state': row['state'], 'version': row['version'],
                'min_rent_cent': row['min_rent_cent'], 'max_rent_cent': row['max_rent_cent'],
                'max_commute_min': row['commute_limit_sec'] // 60 if row['commute_limit_sec'] is not None else None,
                'max_walk_min': row['walk_limit_sec'] // 60 if row['walk_limit_sec'] is not None else None,
                'target_snapshot': decode(row['target_snapshot']), 'target_confirmed_at': row['target_confirmed_at'],
                'created_at': row['created_at'], 'preferences': preferences}

    def workspace(self, owner=None):
        row = self.db.one('SELECT * FROM profiles WHERE owner_hash=?', (owner,)) if owner else None
        if row:
            self.profile(owner)
        tasks=self.db.all('SELECT task_id FROM tasks WHERE profile_id=? ORDER BY created_at DESC,task_id DESC LIMIT 30',(row['profile_id'],)) if row else []
        recent=[self.task(owner,item['task_id']) for item in tasks]
        active=[t for t in recent if not t['legacy_policy'] and t['state'] in ('queued','running','waiting_user','failed')]
        current=next((t for t in active if t['state'] in ('queued','running','waiting_user')),None) or next((t for t in recent if not t['legacy_policy']),None)
        return {'schema_version':1,'profile':self.profile_public(row),'fixed_config':self.settings.fixed_config(),
                'search_defaults':self.settings.search_defaults(),'active_tasks':active,'recent_tasks':recent,'current_task_id':current['task_id'] if current else None}

    def initialize(self, connection, owner):
        existing = self.db.one('SELECT * FROM profiles WHERE owner_hash=?', (owner,))
        if existing:
            return self.workspace(owner)
        batch = self.db.one('SELECT import_id FROM imports ORDER BY import_id DESC LIMIT 1')
        connection.execute('INSERT INTO profiles(profile_id,owner_hash,import_id,state,target_snapshot,created_at) VALUES(?,?,?,?,?,?)',
                           (identifier('profile'), owner, batch['import_id'] if batch else None, 'draft', '{}', now()))
        return self.workspace(owner)

    def mutate(self, owner, operation, key, body, action):
        if not key or len(key) > 200:
            raise DomainError('INVALID_REQUEST', '写请求需要有效的Idempotency-Key。', 422)
        request_hash = digest(body)
        with self.db.transaction() as connection:
            receipt = self.db.one('SELECT * FROM command_receipts WHERE owner_hash=? AND operation=? AND idempotency_key=?', (owner, operation, key))
            if receipt:
                if receipt['request_hash'] != request_hash:
                    raise DomainError('IDEMPOTENCY_CONFLICT', '同一幂等键不能用于不同请求。')
                if receipt['expires_at'] > now():
                    return decode(receipt['result_json'])
                connection.execute('DELETE FROM command_receipts WHERE owner_hash=? AND operation=? AND idempotency_key=?', (owner, operation, key))
            result = action(connection)
            expires = (datetime.now(timezone.utc) + timedelta(days=self.settings.receipt_days)).isoformat()
            connection.execute('INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)',
                               (owner, operation, key, request_hash, encode(result), now(), expires))
            return result

    def confirm_target(self, connection, owner, data, selected=None):
        row = self.profile(owner, data.profile_id)
        self.version(row, data.version)
        config = self.settings.fixed_config()
        if row['state'] != 'draft' or (selected is None and data.config_hash != config['config_hash']) or not config['target_poi'] or not config['location']:
            raise DomainError('INVALID_STATE', '目标配置已改变或画像已锁定，请重新读取。')
        if selected is not None:
            config = {**config, **selected}
        connection.execute('UPDATE profiles SET target_snapshot=?,target_confirmed_at=?,version=version+1 WHERE profile_id=?',
                           (encode(config), now(), row['profile_id']))
        return self.profile_public(self.profile(owner))

    def save_profile(self, connection, owner, data):
        row = self.profile(owner, data.profile_id)
        self.version(row, data.version)
        if row['state'] != 'draft' or not row['target_confirmed_at']:
            raise DomainError('INVALID_STATE', '请先确认目标；锁定画像不能原地修改。')
        if not self.settings.target_covered(decode(row['target_snapshot'])):
            raise DomainError('SOURCE_COVERAGE_UNAVAILABLE', '缺少该通勤目标的房源数据；当前仅覆盖香港中文大学（深圳）。输入已保留。', 409)
        batch = self.db.one('SELECT import_id FROM imports ORDER BY import_id DESC LIMIT 1')
        if not batch:
            raise DomainError('SOURCE_UNAVAILABLE', '尚未导入冻结源批次；草稿已保留。', 503)
        connection.execute("UPDATE profiles SET state='locked',import_id=?,min_rent_cent=?,max_rent_cent=?,commute_limit_sec=?,walk_limit_sec=?,version=version+1 WHERE profile_id=?",
                           (batch['import_id'], data.min_rent_cent, data.max_rent_cent, data.max_commute_min * 60, data.max_walk_min * 60, row['profile_id']))
        for order, item in enumerate(data.preferences):
            connection.execute('INSERT INTO preferences VALUES(?,?,?,?,?,?)',
                               (identifier('pref'), row['profile_id'], item.kind, item.original_text, item.requirement, order))
        return self.profile_public(self.profile(owner))

    def reset(self, connection, owner, data):
        row = self.profile(owner, data.profile_id)
        self.version(row, data.version)
        if data.old_profile_id != row['profile_id']:
            raise DomainError('PROFILE_CHANGED', '重置请求不属于当前画像。')
        connection.execute("UPDATE tasks SET state='cancelled',version=version+1 WHERE profile_id=?", (row['profile_id'],))
        connection.execute('DELETE FROM profiles WHERE profile_id=?', (row['profile_id'],))
        # Delete other receipts that could return deleted business payloads; keep reset receipts for replay.
        connection.execute("DELETE FROM command_receipts WHERE owner_hash=? AND operation NOT LIKE '%/profile/reset'", (owner,))
        return self.initialize(connection, owner)

    def owned_record(self, owner, record_id):
        profile = self.profile(owner)
        row = self.db.one('SELECT * FROM records WHERE record_id=? AND profile_id=?', (record_id, profile['profile_id']))
        if not row:
            raise DomainError('NOT_FOUND', '未找到房源。', 404)
        return row

    def owned_task(self, owner, task_id):
        profile = self.profile(owner)
        row = self.db.one('SELECT * FROM tasks WHERE task_id=? AND profile_id=?', (task_id, profile['profile_id']))
        if not row:
            raise DomainError('NOT_FOUND', '未找到任务。', 404)
        return row

    def task(self, owner, task_id):
        with self.db.lock:
            return self._task_view(owner,task_id)

    def _task_view(self, owner, task_id):
        row = self.owned_task(owner, task_id)
        progress = decode(row['progress'])
        samples=[]
        for original in progress.get('samples',[]):
            sample=dict(original)
            if sample.get('outcome')=='screened' and sample.get('record_id'):
                brief=self.db.one("SELECT content FROM reports WHERE record_id=? AND kind='brief' AND generated_at<=? ORDER BY generated_at DESC,report_id DESC LIMIT 1",(sample['record_id'],row['updated_at']))
                content=decode(brief['content']) if brief else {}
                if content.get('technical'):sample['outcome']='technical_failure'
                elif content.get('reason_code')=='community':sample['outcome']='needs_location'
            samples.append(sample)
        counters = progress.get('counters')
        if counters:
            counters = {region: {**values, 'technical_failure_count':sum(sample.get('region')==region and sample.get('outcome')=='technical_failure' for sample in samples), 'business_screened_count':sum(sample.get('region')==region and sample.get('outcome')=='screened' for sample in samples), 'skipped_count': sum(sample.get('region') == region and sample.get('outcome') == 'skipped' for sample in samples), 'reused_count': sum(
                sample.get('region') == region and sample.get('outcome') == 'duplicate'
                for sample in samples)} for region, values in counters.items()}
        display_record = row['record_id']
        if display_record is None and row['state'] == 'completed':
            results = progress.get('result', {}).get('record_ids', [])
            display_record = progress.get('last_record_view',{}).get('record_id') or (results[-1] if results else None)
        listing = self.db.one('SELECT p.note_id,p.title FROM records r JOIN posts p ON p.post_key=r.post_key WHERE r.record_id=? AND r.profile_id=?', (display_record, row['profile_id'])) if display_record else None
        pending_note=(progress.get('current_sample') or {}).get('note_id') or progress.get('note_id')
        if not listing and pending_note:
            listing=self.db.one('SELECT note_id,title FROM posts WHERE import_id=? AND note_id=?',
                                (progress['config_snapshot']['import_id'],pending_note))
        step_rows = self.db.all('SELECT step_name,state,result FROM steps WHERE record_id=?', (display_record,)) if display_record else []
        collections={s['step_name']:decode(s['result']).get('collection_status') for s in step_rows if s['state']=='succeeded' and s['step_name'] in ('comments','author','community','preference')}
        step_states = {}
        for step in step_rows:
            previous = step_states.get(step['step_name'])
            if previous == 'failed' or step['state'] == 'failed':
                step_states[step['step_name']] = 'failed'
            elif previous == 'running' or step['state'] == 'running':
                step_states[step['step_name']] = 'running'
            else:
                step_states[step['step_name']] = step['state']
        if progress.get('last_record_view') and (not row['record_id'] or row['state'] in ('completed','cancelled')):
            step_states=progress['last_record_view']['step_states']
            collections=progress['last_record_view'].get('collections',{})
        public_snapshot=dict(progress.get('config_snapshot', {}))
        legacy=row['mode']=='auto' and public_snapshot.get('station_selection_strategy')!='manual_station_times_v1'
        public_error=progress.get('last_error')
        recovery=None
        if row['state']=='failed' and (public_error or {}).get('code')=='STATION_SNAPSHOT_NOT_COLLECTED':
            try:
                from .station_table import StationTableRepository
                selection=progress.get('station_selection') or StationTableRepository.select(public_snapshot['target'],public_snapshot['commute_limit_sec'],progress['config_snapshot']['station_selection'])
                coverage=self.source.station_coverage(public_snapshot['import_id'],selection['stations'])
                progress['station_selection']={**selection,'snapshot_coverage':coverage}
                ready=bool(coverage['covered_count']) or not selection['stations']
                recovery={'action':'retry' if ready else 'prepare_source','can_retry':ready,'coverage':coverage,
                    'label':'重新核对范围并继续原任务' if ready else '完善来源后重新检查',
                    'reason':'当前冻结版本的绑定已恢复；可保留原任务及首轮进度继续。' if ready else '当前冻结来源仍缺站点快照；重复执行不会补采。完善同版本关联可继续原任务；更换源版本或站点表需先明确采用，再新建任务。'}
                public_error={**public_error,'message':recovery['reason'],'retryable':ready,'details':{'snapshot_coverage':coverage}}
            except DomainError as e:
                recovery={'action':'prepare_source','can_retry':False,'label':'完善来源后重新检查','reason':e.message}
        if legacy and row['state']=='failed':
            public_error={'code':'LEGACY_DISCOVERY_POLICY','message':'此旧任务使用已撤回的站点方案，请按新人工表新建搜索；原结果保留。','retryable':False,'details':{'historical_error_code':(public_error or {}).get('code')}}
        if 'station_selection' in public_snapshot:
            public_snapshot['station_selection']={k:v for k,v in public_snapshot['station_selection'].items() if k!='table'}
        resolution=None
        location_wait=progress.get('current_step')=='community_prompt' or (public_error or {}).get('code')=='LOCATION_UNCONFIRMED'
        if not legacy and row['state'] in ('failed','waiting_user') and location_wait and row['record_id']:
            from .location import options
            post=self.db.one('SELECT p.note_id,p.title,p.body,p.url,p.quality FROM posts p JOIN records r ON r.post_key=p.post_key WHERE r.record_id=?',(row['record_id'],))
            if post:
                quality = decode(post.pop('quality'))
                link = post.get('url')
                try:
                    if quality.get('original_url_present') is False or not link or parse_link(link) != post['note_id']:
                        link = None
                except (DomainError, ValueError):
                    link = None
                post['original_post_url'] = link
            resolution={'candidates':options(self.db,row,progress),'allow_name_correction':True,'post':post}
        if listing and display_record:
            record=self.db.one('SELECT community_supplement FROM records WHERE record_id=?',(display_record,))
            body=self.db.one("SELECT result FROM steps WHERE record_id=? AND step_name='body' AND state='succeeded'",(display_record,))
            listing={**listing,'community_name':record['community_supplement'] or (decode(body['result']).get('community_name') if body else None)}
        legacy=row['mode']=='auto' and public_snapshot.get('station_selection_strategy')!='manual_station_times_v1'
        stage_task=row;stage_context={**progress,'collection_states':collections};stage_error=public_error
        if row['state'] in ('completed','cancelled') and display_record:
            last=progress.get('last_record_view',{})
            brief=self.db.one("SELECT content FROM reports WHERE record_id=? AND kind='brief' AND generated_at<=? ORDER BY generated_at DESC,report_id DESC LIMIT 1",(display_record,row['updated_at']))
            content=decode(brief['content']) if brief else {}
            outcome=('completed' if row['mode']!='auto' and progress.get('result',{}).get('outcome')=='completed' else 'skipped' if content.get('user_skipped') or last.get('outcome')=='skipped' else 'technical_failure' if content.get('technical') else 'needs_location' if content.get('reason_code')=='community' else last.get('outcome') or ('screened' if content else 'completed'))
            stage_context={**progress,'collection_states':collections,'current_step':last.get('stopped_step') or content.get('stopped_step') or 'summary','record_outcome':outcome}
            stage_error={'message':content.get('reason_text') or '该房源调查已结束'}
            if outcome=='technical_failure' and stage_context['current_step'] not in step_states:
                step_states[stage_context['current_step']]='failed'
        result=dict(progress.get('result') or {})
        result['summary']={key:sum(x.get('outcome')==key for x in samples) for key in ('screened','technical_failure','skipped','needs_location','not_rental','duplicate','invalid_id')}
        result['summary']['candidate_added']=sum(v.get('candidate_added_count',0) for v in (counters or {}).values())
        if counters is None and row['record_id']:
            result['summary']['candidate_added']=int(bool(self.db.one("SELECT report_id FROM reports WHERE record_id=? AND kind='full'",(row['record_id'],))))
            if not result['summary']['candidate_added'] and stage_context.get('record_outcome') in result['summary']:result['summary'][stage_context['record_outcome']]=1
        record_results=self.task_record_results(row,progress,samples)
        result['summary']['recovered_candidates']=sum(bool(r.get('recovery')) for r in record_results)
        return {'schema_version': 1, 'config_snapshot': public_snapshot,'legacy_policy':legacy,'display_kind':'current' if row['state'] not in ('completed','cancelled') and (row['record_id'] or progress.get('current_sample') or pending_note) else 'last_result',
                'recovery':recovery,'search_preflight':progress.get('search_preflight'),
                'policy_transition':progress.get('policy_transition'),
                'record_results':record_results,
                'stage_progress':self.stage_progress(stage_task,stage_context,step_states,stage_error),
                'can_skip':not legacy and row['mode']=='auto' and (row['state']=='failed' or row['state']=='waiting_user' and location_wait) and bool(progress.get('current_sample')) and bool(row['record_id'] or listing) and (public_error or {}).get('code') not in GLOBAL_TASK_ERRORS,
                'location_resolution':resolution,
                'current_listing': dict(listing) if listing else None,
                'step_states': step_states,
                **{key: row[key] for key in ('task_id', 'profile_id', 'record_id', 'mode', 'state', 'version', 'updated_at')},
                'round': progress.get('round'), 'current_step': progress.get('current_step'),
                'counters': counters, 'pending_action': progress.get('pending_action'),
                'events': progress.get('events', []), 'error': public_error,
                'planning_context': progress.get('config_snapshot', {}).get('planning_context'),
                'station_selection': self.selection_summary(progress),
                'result': result}

    def task_record_results(self, task, progress, samples):
        """Per-post outcomes, bounded by this task's saved completion time."""
        results=[];seen=set()
        if not samples and task['record_id'] and task['state'] in ('completed','cancelled'):
            samples=[{'record_id':task['record_id'],'outcome':progress.get('result',{}).get('outcome'),'region':None}]
        for sample in samples:
            rid=sample.get('record_id')
            if not rid or rid in seen:continue
            seen.add(rid)
            post=self.db.one('SELECT p.note_id,p.title FROM records r JOIN posts p ON p.post_key=r.post_key WHERE r.record_id=? AND r.profile_id=?',(rid,task['profile_id']))
            if not post:continue
            brief=self.db.one("SELECT content FROM reports WHERE record_id=? AND kind='brief' AND generated_at<=? ORDER BY generated_at DESC,report_id DESC LIMIT 1",(rid,task['updated_at']))
            content=decode(brief['content']) if brief else {}
            completed=content.get('completed_steps',[])
            states={s['step_name']:'succeeded' for s in completed if s['step_name']!='map_http'}
            stopped=content.get('stopped_step')
            outcome=sample.get('outcome')
            if content.get('user_skipped'):outcome='skipped'
            elif content.get('technical'):outcome='technical_failure'
            if outcome=='completed':
                states={name:'succeeded' for name in ('classification','body','map','comments','author','community','summary')}
                full=self.db.one("SELECT content FROM reports WHERE record_id=? AND kind='full'",(rid,))
                if full and any(i.get('stage')=='preference' for i in decode(full['content']).get('investigations',[])):states['preference']='succeeded'
            saved_steps=list(states)
            if outcome=='technical_failure' and stopped:states[stopped]='failed'
            context={'current_step':stopped,'record_outcome':outcome}
            if outcome=='completed' and full:
                context['collection_states']={i['stage']:i['result'].get('collection_status') for i in decode(full['content']).get('investigations',[])}
            elif content.get('investigations'):
                context['collection_states']={i['stage']:i['result'].get('collection_status') for i in content['investigations']}
            reason=content.get('reason_text') or ('已生成完整报告' if outcome=='completed' else '保留原处理记录')
            recovered=self.db.one("SELECT report_id,generated_at FROM reports WHERE record_id=? AND kind='full' AND generated_at>?",(rid,task['updated_at'])) if outcome!='completed' else None
            results.append({**post,'record_id':rid,'round':(1 if sample.get('region')=='near_target' else 2) if task['mode']=='auto' else None,
                'outcome':outcome,'stopped_step':stopped,'reason_code':content.get('reason_code'),'reason':reason,
                'failure_phase':'source_read' if content.get('reason_code')=='NECESSARY_SOURCE_FAILED' else None,
                'completed_steps':saved_steps,'stage_progress':self.stage_progress(task,context,states,{'message':reason}),
                'recovery':{'state':'completed','full_report_id':recovered['report_id'],'generated_at':recovered['generated_at']} if recovered else None,
                'resume_hint':('仅因作者或原帖评论资料缺失中断，可从分析历史按新容缺规则继续，无需补采；复用成功步骤。' if content.get('reason_code')=='NECESSARY_SOURCE_FAILED' and stopped in ('author','comments') else '先补齐必要核心来源资料，再从分析历史继续；复用已完成步骤，不重复调用。') if content.get('technical') else '可从分析历史恢复，保留已完成步骤。'})
        return results

    def stage_progress(self, task, progress, states, error):
        groups=[('classification',['classification']),('basics',['body','map','comments']),('author',['author']),
                ('community',['community']),('preference',['preference']),('summary',['summary'])]
        labels=['帖子分类','正文、通勤与原帖评论','作者调查','小区调查','偏好专项','综合判断与报告']
        current=progress.get('current_step');blocked_by=None;result=[]
        for (key,steps),label in zip(groups,labels):
            required=not (key=='preference' and not self.db.one("SELECT preference_id FROM preferences WHERE profile_id=? AND kind='custom'",(task['profile_id'],)))
            active=current in steps or key=='basics' and current in ('body_gate','community_prompt')
            if active and progress.get('record_outcome') in ('screened','technical_failure','skipped','needs_location'):
                status=progress['record_outcome'];reason=(error or {}).get('message') or '该房源尚未完成'
            elif not required:
                reached = current in ('preference', 'summary') or any(states.get(s) in ('running', 'succeeded', 'failed') for s in ('preference', 'summary'))
                if reached and all(item['handled'] for item in result):
                    status='not_required';reason='没有自定义偏好，无需专项调查'
                else:
                    status='pending';reason='尚未轮到本阶段'
            elif active and task['state'] in ('failed','waiting_user','cancelled'):
                status={'failed':'failed','waiting_user':'waiting_user','cancelled':'cancelled'}[task['state']]
                reason=(error or {}).get('message') or (progress.get('pending_action') or {}).get('prompt') or '任务已取消'
            elif any(states.get(step)=='failed' for step in steps):status='failed';reason=(error or {}).get('message') or '本阶段未完成，可从分析历史恢复'
            elif all(states.get(step)=='succeeded' for step in steps):
                qualities=[progress.get('collection_states',{}).get(s) for s in steps]
                status=next((q for q in ('failed','not_collected','partial','skipped_optional') if q in qualities),'succeeded')
                status='degraded' if status=='failed' else status
                reason={'degraded':'辅助资料或分析失败已登记，保留可用原文继续S6','not_collected':'资料未采集，已登记缺口，不阻断S6','partial':'资料部分取得，保留已有内容及缺失说明','skipped_optional':'未执行专项检索，S6使用现有全部证据，不冒称已搜索0条'}.get(status,'已完成并保存')
            elif blocked_by:status='blocked';reason='前置阶段“'+blocked_by+'”尚未完成，本阶段未执行'
            elif active and task['state'] in ('queued','running'):status='running';reason='正在处理'
            else:status='pending';reason='尚未执行'
            if status in ('failed','waiting_user','cancelled','screened','technical_failure','skipped','needs_location'):blocked_by=label
            result.append({'stage':key,'label':label,'state':status,'reason':reason,'handled':status in ('succeeded','partial','not_collected','degraded','skipped_optional','not_required'),'steps':{step:states.get(step,'pending') for step in steps},'collection_states':{s:progress.get('collection_states',{}).get(s) for s in steps}})
        return result

    @staticmethod
    def selection_summary(progress):
        selection = progress.get('station_selection')
        if not selection:
            return progress.get('station_selection_status')
        keys = ('strategy', 'table_version', 'table_hash', 'time_basis', 'source_note', 'transport_mode',
                'coverage_limit_sec', 'table_coverage_limited', 'commute_limit_sec', 'plan_revision',
                'selection_state', 'outcome', 'snapshot_coverage')
        return {**{key: selection.get(key) for key in keys}, 'station_count': len(selection.get('stations', []))}

    def _new_task(self, connection, owner, data, key, mode, record=None, note_id=None):
        profile = self.profile(owner, data.profile_id)
        self.version(record or profile, data.version)
        if profile['state'] != 'locked' or profile['walk_limit_sec'] is None:
            raise DomainError('INVALID_STATE', '请先保存V1.37画像，旧到站步行值不能自动迁入。')
        active = self.db.all("SELECT * FROM tasks WHERE profile_id=? AND state IN('queued','running','waiting_user')", (profile['profile_id'],))
        for task in active:
            progress = decode(task['progress'])
            if (mode == 'auto' and task['mode'] == 'auto') or (note_id and progress.get('note_id') == note_id) or (record and task['record_id'] == record['record_id']):
                raise DomainError('RECORD_BUSY', '已有任务正在处理，请查看原任务。', task_id=task['task_id'])
        today = now()[:10]
        count = self.db.one('SELECT COUNT(*) AS n FROM tasks WHERE profile_id=? AND created_at>=?', (profile['profile_id'], today))['n']
        if count >= self.settings.task_limit_per_day:
            raise DomainError('RATE_LIMITED', '当前画像今日任务额度已用完。', 429)
        snapshot = {'planning_context': self.settings.planning_context(), 'target': decode(profile['target_snapshot']),
                    'station_selection_strategy': 'manual_station_times_v1', 'station_selection': self.station_tables.freeze(),
                    'import_id': profile['import_id'], 'commute_limit_sec': profile['commute_limit_sec'],
                    'walk_limit_sec': profile['walk_limit_sec'], 'search': self.settings.search_defaults(),
                    'route_policy_version': 'v1.37', 'model_name': self.settings.model_name,
                    'prompt_version': self.prompt_version,'auxiliary_policy':'available_evidence_v1'}
        preflight=None
        if mode=='auto':
            preflight=self.search_preflight(owner,snapshot,internal=True)
            if not preflight['can_start']:
                e=preflight['error'];raise DomainError(e['code'],e['message'],409,False,preflight={k:v for k,v in preflight.items() if not k.startswith('_')})
            token=getattr(data,'preflight_token',None)
            if token and token!=preflight['preflight_token']:
                raise DomainError('SEARCH_PREFLIGHT_CHANGED','搜索范围已变化，请重新检查并确认。',409)
            if preflight['requires_partial_confirmation'] and (not getattr(data,'allow_partial',False) or token!=preflight['preflight_token']):
                raise DomainError('SEARCH_COVERAGE_CONFIRMATION_REQUIRED','部分入选站点缺少完整快照，请先确认仅处理现有覆盖；未创建任务或调用模型。',409,preflight={k:v for k,v in preflight.items() if not k.startswith('_')})
        confirmations={}
        policy_transition=None
        if record:
            note_id = self.db.one('SELECT note_id FROM posts WHERE post_key=?', (record['post_key'],))['note_id']
            previous = self.db.one('SELECT * FROM tasks WHERE profile_id=? AND instr(progress,?)>0 ORDER BY created_at DESC LIMIT 1', (profile['profile_id'], record['record_id']))
            if previous:
                previous_progress=decode(previous['progress'])
                confirmations=previous_progress.get('location_confirmations',{})
                snapshot = previous_progress['config_snapshot']
                if snapshot.get('route_policy_version') != 'v1.37':
                    raise DomainError('LEGACY_POLICY', '旧任务不能静默切换新路线规则，请保留历史并重置画像。')
                if snapshot['import_id']!=profile['import_id']:
                    from .source_repair import author_revision_path
                    if not author_revision_path(self.db,snapshot['import_id'],profile['import_id']):
                        raise DomainError('SOURCE_REVISION_UNCONFIRMED','当前来源不是已明确采用的作者补充版本，不能复用原调查。')
                    snapshot={**snapshot,'import_id':profile['import_id'],'source_revision':{'from_import_id':snapshot['import_id'],'to_import_id':profile['import_id'],'kind':'author_only','preserved_successful_steps':True}}
                if snapshot.get('auxiliary_policy')!='available_evidence_v1':
                    policy_transition={'from':snapshot.get('auxiliary_policy','legacy_required_sources'),'to':'available_evidence_v1','explicit_action':'record_resume','successful_steps_reused':True,'from_prompt_version':snapshot.get('prompt_version'),'to_prompt_version':self.prompt_version}
                    snapshot={**snapshot,'auxiliary_policy':'available_evidence_v1','prompt_version':self.prompt_version}
        counters = {name: dict(hit_count=0, attempt_count=0, eligible_count=0, candidate_added_count=0)
                    for name in ('near_target', 'near_metro')} if mode == 'auto' else None
        progress = {'schema_version': 1, 'config_snapshot': snapshot, 'note_id': note_id,
                    'policy_transition':policy_transition,
                    'round': 1 if mode == 'auto' else None, 'counters': counters, 'samples': [], 'events': [],
                    'location_confirmations':{record['record_id']:confirmations[record['record_id']]} if record and record['record_id'] in confirmations else {},
                    'cursors': {'near_target': 0, 'near_metro': 0}, 'plan': {}, 'pending_action': None,
                    'last_error': None, 'current_step': 'classification',
                    'result': {'outcome': None, 'record_ids': [record['record_id']] if record else [], 'brief_ids': []}}
        task_id = identifier('task')
        if preflight:
            progress['search_preflight']={k:v for k,v in preflight.items() if not k.startswith('_')}
            progress['station_selection']=preflight['_selection']
            progress['station_selection_key']=digest({'target':snapshot['target'],'commute_limit_sec':snapshot['commute_limit_sec'],
                'strategy':snapshot['station_selection_strategy'],'table_hash':snapshot['station_selection'].get('table_hash')})
        connection.execute('INSERT INTO tasks(task_id,profile_id,record_id,mode,state,idempotency_key,request_hash,progress,created_at,queued_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (task_id, profile['profile_id'], record['record_id'] if record else None, mode, 'queued', key,
                            digest(data.model_dump()), encode(progress), now(), now(), now()))
        return self.task(owner, task_id)

    def search_preflight(self, owner, snapshot=None, internal=False):
        profile=self.profile(owner)
        if profile['state']!='locked':raise DomainError('INVALID_STATE','请先保存并锁定画像。')
        if snapshot is None:
            snapshot={'target':decode(profile['target_snapshot']),'import_id':profile['import_id'],
                'commute_limit_sec':profile['commute_limit_sec'],'station_selection':self.station_tables.freeze(),
                'planning_context':self.settings.planning_context()}
        result={'can_start':False,'requires_partial_confirmation':False,'planning_context':snapshot['planning_context'],
                'table_version':snapshot['station_selection'].get('table_version'),'table_time_basis':None,'coverage':None,'error':None}
        try:
            from .station_table import StationTableRepository
            selector=getattr(self,'station_selector',StationTableRepository.select)
            context={**snapshot['planning_context'],'station_selection':snapshot['station_selection']}
            selection=selector(snapshot['target'],snapshot['commute_limit_sec'],context) if hasattr(self,'station_selector') else selector(snapshot['target'],snapshot['commute_limit_sec'],snapshot['station_selection'])
            coverage=self.source.station_coverage(snapshot['import_id'],selection['stations'])
            result.update(table_time_basis=selection.get('time_basis'),coverage=coverage,
                table_coverage_limited=selection.get('table_coverage_limited',False),table_coverage_limit_sec=selection.get('coverage_limit_sec'),
                can_start=not selection['stations'] or coverage['covered_count']>0,
                requires_partial_confirmation=bool(selection['stations']) and coverage['state']=='partial')
            selection={**selection,'snapshot_coverage':coverage};result['_selection']=selection
            if not result['can_start']:result['error']={'code':'STATION_SNAPSHOT_NOT_COLLECTED','message':'启动前检查：入选站点均无可用且已绑定的完整快照。请先补齐来源，不会先执行第一轮或将缺失当成零结果。'}
        except DomainError as e:result['error']={'code':e.code,'message':e.message}
        result['preflight_token']=digest({'profile_id':profile['profile_id'],'version':profile['version'],'snapshot':{k:snapshot[k] for k in ('target','import_id','commute_limit_sec','station_selection','planning_context')},'coverage':result['coverage']})
        return result if internal else {k:v for k,v in result.items() if not k.startswith('_')}

    def submit_search(self, connection, owner, data, key):
        return self._new_task(connection, owner, data, key, 'auto')

    def submit_import(self, connection, owner, data, key):
        profile = self.profile(owner, data.profile_id)
        note_id = parse_link(data.url)
        post = self.source.post(profile['import_id'], note_id)
        record = self.db.one('SELECT * FROM records WHERE profile_id=? AND post_key=?', (profile['profile_id'], post['post_key']))
        if record:
            if self.db.one("SELECT report_id FROM reports WHERE record_id=? AND kind='full'", (record['record_id'],)):
                return {'disposition': 'existing', 'record': self.record(owner, record['record_id'])}
            raise DomainError('RECORD_BUSY', '该帖子已有调查记录，请从历史继续分析或查看原任务。', record_id=record['record_id'])
        return self._new_task(connection, owner, data, key, 'manual', note_id=note_id)

    def resume(self, connection, owner, record_id, data, key):
        row = self.owned_record(owner, record_id)
        self.version(row, data.version)
        if row['list_status'] != 'screened':
            raise DomainError('INVALID_STATE', '只有被筛除房源可继续分析。')
        return self._new_task(connection, owner, data, key, 'resume', row)

    def retry(self, connection, owner, task_id, data):
        row = self.owned_task(owner, task_id)
        self.profile(owner, data.profile_id)
        self.version(row, data.version)
        if row['state'] != 'failed':
            raise DomainError('INVALID_STATE', '只有失败任务可重试。')
        progress = decode(row['progress'])
        error=progress.get('last_error') or {}
        if row['mode']=='auto' and progress['config_snapshot'].get('station_selection_strategy')!='manual_station_times_v1':
            raise DomainError('LEGACY_DISCOVERY_POLICY','旧站点方案已撤回，请按人工表新建搜索，原结果保留。',409)
        if row['mode']=='auto' and error.get('code')=='STATION_SNAPSHOT_NOT_COLLECTED':
            from .station_table import StationTableRepository
            selection=progress.get('station_selection') or StationTableRepository.select(progress['config_snapshot']['target'],progress['config_snapshot']['commute_limit_sec'],progress['config_snapshot']['station_selection'])
            coverage=self.source.station_coverage(progress['config_snapshot']['import_id'],selection['stations'])
            if selection['stations'] and not coverage['covered_count']:
                raise DomainError('STATION_SNAPSHOT_NOT_COLLECTED','当前冻结来源仍无可用站点快照。请先完善来源，重复重试不会自动补采或改用新版本。',409,snapshot_coverage=coverage)
            progress['station_selection']={**selection,'snapshot_coverage':coverage}
            if not progress['counters']['near_metro']['attempt_count']:progress['plan'].pop('near_metro',None)
        if row['mode']=='auto' and error.get('code') in ('SOURCE_COVERAGE_MISSING','SOURCE_SNAPSHOT_INCOMPLETE'):
            latest=self.db.one('SELECT import_id FROM imports ORDER BY import_id DESC LIMIT 1')
            old_import=progress['config_snapshot']['import_id']
            if latest and latest['import_id']!=old_import:
                claimed=progress.get('current_sample') or progress.get('samples') or any(v.get('attempt_count',0) for v in progress.get('counters',{}).values())
                records=self.db.one('SELECT COUNT(*) AS n FROM records WHERE profile_id=?',(row['profile_id'],))['n']
                if not claimed and not records and not row.get('record_id'):
                    # Validate before switching. Incomplete/unmapped data remains a real error.
                    self.source.search(latest['import_id'],'near_target',target=progress['config_snapshot']['target'])
                    progress['config_snapshot']['import_id']=latest['import_id']
                    progress['plan']={}
                    progress['round']=1
                    progress['cursors']={'near_target':0,'near_metro':0}
                    for counts in progress.get('counters',{}).values():
                        for key in counts:counts[key]=0
                    connection.execute('UPDATE profiles SET import_id=?,version=version+1 WHERE profile_id=?',(latest['import_id'],row['profile_id']))
                    progress['events'].append({'seq':progress.get('event_seq',0)+1,'type':'source_revision_applied','step':progress.get('current_step'),'at':now(),'data':{'from_import_id':old_import,'to_import_id':latest['import_id']}})
                    progress['event_seq']=progress.get('event_seq',0)+1
        progress['last_error'] = None
        progress.pop('executor_token', None)
        connection.execute("UPDATE tasks SET state='queued',progress=?,version=version+1,queued_at=?,updated_at=? WHERE task_id=?", (encode(progress), now(), now(), task_id))
        return self.task(owner, task_id)

    @staticmethod
    def task_event(progress,kind,**data):
        progress['event_seq']=progress.get('event_seq',0)+1
        progress['events'].append({'seq':progress['event_seq'],'type':kind,'step':progress.get('current_step'),'at':now(),'data':data})

    def skip_current(self,connection,owner,task_id,data):
        task=self.owned_task(owner,task_id);self.profile(owner,data.profile_id);self.version(task,data.version)
        progress=decode(task['progress'])
        if task['mode']!='auto' or task['state'] not in ('failed','waiting_user') or not progress.get('current_sample') or task['state']=='waiting_user' and (progress.get('pending_action') or {}).get('kind') not in ('community','location'):
            raise DomainError('INVALID_STATE','只能跳过自动搜索中已经失败的当前房源。')
        if progress['config_snapshot'].get('station_selection_strategy')!='manual_station_times_v1':
            raise DomainError('LEGACY_DISCOVERY_POLICY','旧自动任务仅供回看，请按新方案新建搜索。')
        if (progress.get('last_error') or {}).get('code') in GLOBAL_TASK_ERRORS:
            raise DomainError('INVALID_STATE','预算、凭据或配置问题影响整个任务，请先修复，不能通过跳过房源隐藏。')
        if task['record_id']:
            record=self.owned_record(owner,task['record_id'])
        else:
            # Classification may fail before a record exists; retain this post for later retry.
            post=self.source.post(progress['config_snapshot']['import_id'],progress['current_sample']['note_id'])
            record=self.db.one('SELECT * FROM records WHERE profile_id=? AND post_key=?',(task['profile_id'],post['post_key']))
            if not record:
                rid=identifier('record')
                connection.execute('INSERT INTO records(record_id,profile_id,post_key,created_at) VALUES(?,?,?,?)',(rid,task['profile_id'],post['post_key'],now()))
                record=self.db.one('SELECT * FROM records WHERE record_id=?',(rid,))
            if record['record_id'] not in progress['result']['record_ids']:progress['result']['record_ids'].append(record['record_id'])
        if self.db.one("SELECT report_id FROM reports WHERE record_id=? AND kind='full'",(record['record_id'],)):
            raise DomainError('INVALID_STATE','完整报告已保存，不改写为跳过。')
        error=progress.get('last_error') or {'code':'LOCATION_REQUIRED','message':'等待确认小区或楼栋地址'}
        brief=self.screen(connection,record['record_id'],error['code'],'用户跳过，原失败原因：'+error['message'],True,False,progress['current_step'])
        content=decode(self.db.one('SELECT content FROM reports WHERE report_id=?',(brief,))['content'])
        content.update(user_skipped=True,failure_snapshot=error)
        connection.execute('UPDATE reports SET content=?,input_hash=? WHERE report_id=?',(encode(content),digest(content),brief))
        progress['result']['brief_ids'].append(brief)
        progress['last_record_view']={'record_id':record['record_id'],'outcome':'skipped','stopped_step':progress['current_step'],'step_states':{r['step_name']:r['state'] for r in self.db.all('SELECT step_name,state FROM steps WHERE record_id=?',(record['record_id'],)) if r['step_name']!='map_http'}}
        sample=progress.pop('current_sample');sample.update(outcome='skipped',record_id=record['record_id'],failure=error)
        progress['samples'].append(sample)
        self.task_event(progress,'record_skipped_by_user',record_id=record['record_id'],reason=error['code'])
        progress.update(current_step='classification',last_error=None,pending_action=None)
        progress.pop('executor_token',None)
        connection.execute("UPDATE tasks SET state='queued',record_id=NULL,progress=?,version=version+1,queued_at=?,updated_at=? WHERE task_id=?",(encode(progress),now(),now(),task_id))
        return self.task(owner,task_id)

    def resolve_location(self,connection,owner,task_id,data):
        task=self.owned_task(owner,task_id);self.profile(owner,data.profile_id);self.version(task,data.version)
        progress=decode(task['progress'])
        if task['state'] not in ('failed','waiting_user') or not task['record_id'] or not (progress.get('current_step')=='community_prompt' or progress.get('current_step')=='map' and (progress.get('last_error') or {}).get('code')=='LOCATION_UNCONFIRMED'):
            raise DomainError('INVALID_STATE','当前任务没有待处理的定位失败。')
        if task['mode']=='auto' and progress['config_snapshot'].get('station_selection_strategy')!='manual_station_times_v1':
            raise DomainError('LEGACY_DISCOVERY_POLICY','请从历史单独恢复该房源，或按新方案新建搜索。')
        record=self.owned_record(owner,task['record_id'])
        if data.poi_id:
            from .location import options
            matches=[item for item in options(self.db,task,progress) if item['id']==data.poi_id]
            if len(matches)!=1:raise DomainError('LOCATION_SELECTION_INVALID','位置不属于本房源已保存的有效高德候选，请重新确认。')
            place=matches[0]
            body=self.db.one("SELECT result FROM steps WHERE record_id=? AND step_name='body' AND state='succeeded'",(record['record_id'],))
            community=record['community_supplement'] or decode(body['result'])['community_name']
            progress.setdefault('location_confirmations',{})[record['record_id']]={**place,'aliases':[community],
                'verified':True,'source_ref':'user-confirmed:'+task_id+':'+place['id'],'confirmed_at':now()}
            self.task_event(progress,'location_confirmed',record_id=record['record_id'],poi_id=place['id'])
        else:
            connection.execute('UPDATE records SET community_supplement=? WHERE record_id=?',(data.community_name.strip(),record['record_id']))
            progress.setdefault('location_confirmations',{}).pop(record['record_id'],None)
            self.task_event(progress,'community_corrected',record_id=record['record_id'],name=data.community_name.strip())
        connection.execute("UPDATE conditions SET result='unknown',decision=NULL,version=version+1 WHERE record_id=? AND condition_code='community' AND result='fail'",(record['record_id'],))
        connection.execute('UPDATE records SET version=version+1 WHERE record_id=?',(record['record_id'],))
        progress['last_error']=None;progress['pending_action']=None;progress.pop('executor_token',None)
        connection.execute("UPDATE tasks SET state='queued',progress=?,version=version+1,queued_at=?,updated_at=? WHERE task_id=?",(encode(progress),now(),now(),task_id))
        return self.task(owner,task_id)

    def screen(self, connection, record_id, code, text, technical=False, retryable=False, step=None):
        conditions = self.db.all('SELECT condition_id FROM conditions WHERE record_id=?', (record_id,))
        completed = self.db.all("SELECT step_name,scope_key FROM steps WHERE record_id=? AND state='succeeded'", (record_id,))
        content = {'schema_version': 1, 'stopped_step': step, 'reason_code': code, 'reason_text': text,
                   'technical': technical, 'retryable': retryable, 'condition_ids': [row['condition_id'] for row in conditions],
                   'evidence_ids': [row['evidence_id'] for row in self.db.all('SELECT evidence_id FROM evidence WHERE record_id=?', (record_id,))],
                   'completed_steps': completed}
        body = self.db.one("SELECT result FROM steps WHERE record_id=? AND step_name='body' AND state='succeeded'", (record_id,))
        if body:
            content['body'] = decode(body['result'])
        content['investigations'] = [{'stage': row['step_name'], 'scope': row['scope_key'], 'result': decode(row['result'])}
            for row in self.db.all("SELECT step_name,scope_key,result FROM steps WHERE record_id=? AND step_name IN('comments','author','community','preference') AND state='succeeded' ORDER BY step_name,scope_key", (record_id,))]
        if any(i['result'].get('auxiliary_policy') for i in content['investigations']):
            from .auxiliary import completeness
            content['data_completeness']=completeness(content['investigations'])
        route = self.db.one("SELECT result FROM steps WHERE record_id=? AND step_name='map' AND scope_key='main' AND state='succeeded'", (record_id,))
        if route:
            content['route'] = decode(route['result'])
        report_id = identifier('brief')
        connection.execute("INSERT INTO reports VALUES(?,?,'brief',?,?,?)", (report_id, record_id, encode(content), digest(content), now()))
        connection.execute("UPDATE records SET list_status='screened',version=version+1 WHERE record_id=?", (record_id,))
        return report_id

    def cancel(self, connection, owner, task_id, data):
        row = self.owned_task(owner, task_id)
        self.profile(owner, data.profile_id)
        self.version(row, data.version)
        if row['state'] in ('completed', 'cancelled'):
            raise DomainError('INVALID_STATE', '任务已经结束。')
        progress = decode(row['progress'])
        if row['record_id']:
            current = self.owned_record(owner, row['record_id'])
            if current['list_status'] is None:
                report_id = self.screen(connection, row['record_id'], 'USER_CANCELLED', '用户取消调查，可从历史继续。')
                progress['result']['brief_ids'].append(report_id)
        progress['result']['outcome'] = 'cancelled'
        progress['pending_action'] = None
        connection.execute("UPDATE tasks SET state='cancelled',progress=?,version=version+1,updated_at=? WHERE task_id=?", (encode(progress), now(), task_id))
        return self.task(owner, task_id)

    def decide(self, connection, owner, condition_id, data):
        condition = self.db.one('SELECT * FROM conditions WHERE condition_id=?', (condition_id,))
        if not condition:
            raise DomainError('NOT_FOUND', '条件不存在。', 404)
        record = self.owned_record(owner, condition['record_id'])
        self.profile(owner, data.profile_id)
        self.version(condition, data.version)
        task = self.db.one("SELECT * FROM tasks WHERE record_id=? AND state='waiting_user' ORDER BY created_at DESC LIMIT 1", (record['record_id'],))
        if not task or decode(task['progress']).get('pending_action', {}).get('condition_id') != condition_id or condition['result'] != 'fail':
            raise DomainError('INVALID_STATE', '只能处理当前待确认的业务不符合条件。')
        connection.execute('UPDATE conditions SET decision=?,decided_at=?,version=version+1 WHERE condition_id=?', (data.decision, now(), condition_id))
        progress = decode(task['progress'])
        progress['pending_action'] = None
        state = 'queued'
        if data.decision == 'abort':
            report_id = self.screen(connection, record['record_id'], 'USER_ABORT', '用户中止当前条件后的分析。', step=progress.get('current_step'))
            progress['result']['brief_ids'].append(report_id)
            progress['result']['outcome'] = 'screened'
            state = 'completed'
        connection.execute('UPDATE tasks SET state=?,progress=?,version=version+1,queued_at=?,updated_at=? WHERE task_id=?',
                           (state, encode(progress), now(), now(), task['task_id']))
        return {'condition': self.condition_public(self.db.one('SELECT * FROM conditions WHERE condition_id=?', (condition_id,))),
                'task': self.task(owner, task['task_id'])}

    def supplement(self, connection, owner, record_id, data):
        record = self.owned_record(owner, record_id)
        self.profile(owner, data.profile_id)
        self.version(record, data.version)
        task = self.db.one("SELECT * FROM tasks WHERE record_id=? AND state='waiting_user'", (record_id,))
        if not task or decode(task['progress']).get('pending_action', {}).get('kind') != 'community' or not data.name.strip():
            raise DomainError('INVALID_STATE', '当前房源未等待小区补录。')
        connection.execute('UPDATE records SET community_supplement=?,version=version+1 WHERE record_id=?', (data.name.strip(), record_id))
        progress = decode(task['progress'])
        progress['pending_action'] = None
        connection.execute("UPDATE tasks SET state='queued',progress=?,version=version+1,queued_at=?,updated_at=? WHERE task_id=?", (encode(progress), now(), now(), task['task_id']))
        return {'record': self.record(owner, record_id), 'task': self.task(owner, task['task_id'])}

    @staticmethod
    def condition_public(row):
        return {**{key: row[key] for key in ('condition_id', 'record_id', 'condition_code', 'preference_id', 'result', 'decision', 'version')},
                'evidence_ids': decode(row['evidence_ids']), 'route_context': decode(row['route_context'])}

    def record(self, owner, record_id):
        row = self.owned_record(owner, record_id)
        post = self.db.one('SELECT note_id,url,published_at,title FROM posts WHERE post_key=?', (row['post_key'],))
        full = self.db.one("SELECT * FROM reports WHERE record_id=? AND kind='full'", (record_id,))
        brief = self.db.one("SELECT report_id,content FROM reports WHERE record_id=? AND kind='brief' ORDER BY generated_at DESC,report_id DESC LIMIT 1", (record_id,))
        brief_content = decode(brief['content']) if brief else {}
        resume_stage = None
        if row['list_status'] == 'screened':
            completed = {item['step_name'] for item in self.db.all("SELECT step_name FROM steps WHERE record_id=? AND state='succeeded'", (record_id,))}
            resume_stage = next((name for name in ('classification', 'body', 'map', 'comments', 'author', 'community', 'preference', 'summary') if name not in completed), 'summary')
        content = decode(full['content']) if full else {}
        adjustments = self.db.one('SELECT * FROM record_manual_adjustments WHERE record_id=?', (record_id,))
        overrides = decode(adjustments['preference_results']) if adjustments else {}
        results = {item['preference_id']: item.get('result', 'unknown') for item in content.get('preferences', [])}
        preference_values = [{**item, 'original_result': results.get(item['preference_id'], 'unknown'),
                              'evidence_ids':next((p.get('evidence_ids',[]) for p in content.get('preferences',[]) if p['preference_id']==item['preference_id']),[]),
                              'result': overrides.get(item['preference_id'], results.get(item['preference_id'], 'unknown')),
                              'manually_updated': item['preference_id'] in overrides}
                             for item in self.db.all('SELECT preference_id,original_text,requirement FROM preferences WHERE profile_id=? ORDER BY display_order', (row['profile_id'],))]
        route = content.get('route')
        if not route:
            step = self.db.one("SELECT result FROM steps WHERE record_id=? AND step_name='map' AND scope_key='main' AND state='succeeded'", (record_id,))
            route = decode(step['result']) if step else None
        summary = None
        if route:
            route_id = route.get('selected_route_id') or route.get('adopted_route_id')
            selected = next((item for item in route['routes'] if item['route_id'] == route_id), None)
            if selected:
                summary = {**{key: selected.get(key) for key in ('route_id', 'mode', 'duration_sec', 'total_walk_sec', 'walking_distance_m', 'transfer_count', 'commute_pass', 'walk_pass', 'joint_pass')},
                           **{key: route.get(key) for key in ('reference_date', 'departure_time', 'timezone', 'queried_at', 'route_policy_version', 'commute_limit_sec', 'walk_limit_sec')}}
        manual_values = {**{key: row['manual_' + key] for key in ('rent_cent', 'monthly_cent', 'initial_cent', 'commute_sec')},
                         'walk_sec': adjustments['walk_sec'] if adjustments else None}
        flags = self.db.one('SELECT * FROM record_time_overrides WHERE record_id=?', (record_id,)) or {}
        current_values, value_sources = dict(manual_values), {}
        for key, route_key, flag in (('commute_sec', 'duration_sec', 'commute_overridden'), ('walk_sec', 'total_walk_sec', 'walk_overridden')):
            estimate = summary.get(route_key) if summary else None
            value = manual_values[key]
            # Existing non-null walking edits and differing commute edits predate provenance tracking.
            overridden = flags.get(flag) or (key == 'walk_sec' and value is not None) or (key == 'commute_sec' and value != estimate)
            if key == 'walk_sec' and not overridden:
                current_values[key] = estimate
            value_sources[key] = 'manual' if overridden else 'map' if estimate is not None else 'unknown'
        viewing = self.db.one('SELECT * FROM viewing WHERE record_id=?', (record_id,))
        risks = content.get('risks', [])
        done = sum(decode(viewing['risk_checks']).values()) if viewing else 0
        catalog=self.evidence_sources(record_id,content.get('evidence_ids')) if full else {}
        disposition=('skipped' if brief_content.get('user_skipped') else 'technical_failure' if brief_content.get('technical') else
                     'needs_location' if brief_content.get('reason_code')=='community' else 'cancelled' if brief_content.get('reason_code') in ('USER_CANCELLED','USER_ABORT') else 'business_screened') if row['list_status']=='screened' else row['list_status'] or 'in_progress'
        return {'schema_version': 1,'disposition':disposition, 'screening_reason': brief_content.get('reason_text') or brief_content.get('summary'),
                'resume_stage': resume_stage, **{key: row[key] for key in ('record_id', 'profile_id', 'list_status', 'community_supplement', 'remark', 'created_at', 'candidate_added_at', 'version')},
                'note_id': post['note_id'], 'source_url': post['url'], 'published_at': post['published_at'], 'title': post['title'],
                'manual_values': manual_values, 'current_values': current_values, 'value_sources': value_sources,
                'preferences': preference_values,
                'route_summary': summary, 'full_report_id': full['report_id'] if full else None,
                'latest_brief_id': brief['report_id'] if brief else None, 'risk_count': len(risks), 'risk_done': done,
                'viewing_conclusion': viewing['conclusion'] if viewing else 'undecided',
                'data_completeness':content.get('data_completeness'),
                'completeness_summary':quality_summary(content.get('data_completeness'),content.get('missing')),
                'evidence_sources':catalog,
                'body_evidence_ids':[eid for eid,x in catalog.items() if x.get('source_kind')=='body' and x.get('is_original')],
                'resume_hint':'这是旧规则下的辅助资料中断；可直接继续分析，无需补齐资料，已有成功步骤复用。' if row['list_status']=='screened' and brief_content.get('reason_code')=='NECESSARY_SOURCE_FAILED' and brief_content.get('stopped_step') in ('author','comments') else None,
                'summary': content.get('summary'), 'conditions': [self.condition_public(item) for item in self.db.all('SELECT * FROM conditions WHERE record_id=? ORDER BY condition_id', (record_id,))]}

    def page(self, owner, status, cursor=None, limit=20):
        profile = self.profile(owner)
        if not 1 <= limit <= 100:
            raise DomainError('INVALID_REQUEST', '分页数量应为1至100。', 422)
        params, extra = [profile['profile_id'], status], ''
        if cursor:
            try:
                decoded = decode(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode())
                stamp, record_id = decoded['time'], decoded['id']
                if not isinstance(stamp, str) or not isinstance(record_id, str):
                    raise ValueError()
            except Exception:
                raise DomainError('INVALID_REQUEST', '分页游标无效。', 422) from None
            extra = ' AND (COALESCE(candidate_added_at,created_at),record_id)<(?,?)'
            params += [stamp, record_id]
        rows = self.db.all('SELECT record_id,COALESCE(candidate_added_at,created_at) AS stamp FROM records WHERE profile_id=? AND list_status=?' + extra +
                            ' ORDER BY stamp DESC,record_id DESC LIMIT ?', (*params, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(encode({'time': rows[-1]['stamp'], 'id': rows[-1]['record_id']}).encode()).decode().rstrip('=') if more else None
        return {'items': [self.record(owner, item['record_id']) for item in rows], 'next_cursor': next_cursor}

    def patch(self, connection, owner, record_id, data):
        record = self.owned_record(owner, record_id)
        self.profile(owner, data.profile_id)
        self.version(record, data.version)
        if record['list_status'] not in ('candidate', 'archived'):
            raise DomainError('INVALID_STATE', '调查未完成，不能编辑候选人工值。')
        if data.group == 'all' and data.preference_results is not None:
            ids = [item.preference_id for item in data.preference_results]
            allowed = {item['preference_id'] for item in self.db.all('SELECT preference_id FROM preferences WHERE profile_id=?', (record['profile_id'],))}
            if len(set(ids)) != len(ids) or not set(ids) <= allowed:
                raise DomainError('INVALID_REQUEST', '偏好结果包含重复项或不属于当前画像。', 422)
        before = self.record(owner, record_id)
        keys = {'cost': ('rent_cent', 'monthly_cent', 'initial_cent'), 'commute': ('commute_sec',), 'remark': ('remark',),
                'all': ('rent_cent', 'monthly_cent', 'initial_cent', 'commute_sec', 'remark')}[data.group]
        columns = [('manual_' + key if key != 'remark' else key, getattr(data, key)) for key in keys]
        connection.execute('UPDATE records SET ' + ','.join(column + '=?' for column, _ in columns) + ',version=version+1 WHERE record_id=?',
                           (*[value for _, value in columns], record_id))
        if data.group == 'all' and {'walk_sec', 'preference_results'} & data.model_fields_set:
            previous = self.db.one('SELECT * FROM record_manual_adjustments WHERE record_id=?', (record_id,))
            overrides = decode(previous['preference_results']) if previous else {}
            if data.preference_results is not None:
                overrides.update({item.preference_id: item.result for item in data.preference_results})
            walking = (data.walk_sec if data.walk_sec != before['current_values']['walk_sec'] or before['value_sources']['walk_sec'] == 'manual' else None) if 'walk_sec' in data.model_fields_set else previous['walk_sec'] if previous else None
            connection.execute('INSERT INTO record_manual_adjustments(record_id,walk_sec,preference_results,updated_at) VALUES(?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET walk_sec=excluded.walk_sec,preference_results=excluded.preference_results,updated_at=excluded.updated_at',
                               (record_id, walking, encode(overrides), now()))
        flags = self.db.one('SELECT * FROM record_time_overrides WHERE record_id=?', (record_id,)) or {}
        changed = {}
        for key, flag in (('commute_sec', 'commute_overridden'), ('walk_sec', 'walk_overridden')):
            supplied = (key == 'commute_sec' and data.group in ('all', 'commute')) or (key == 'walk_sec' and data.group == 'all' and key in data.model_fields_set)
            changed[flag] = int(bool(flags.get(flag)) or (supplied and getattr(data, key) != before['current_values'][key]))
        connection.execute('INSERT INTO record_time_overrides VALUES(?,?,?) ON CONFLICT(record_id) DO UPDATE SET commute_overridden=excluded.commute_overridden,walk_overridden=excluded.walk_overridden',
                           (record_id, changed['commute_overridden'], changed['walk_overridden']))
        return self.record(owner, record_id)

    def archive(self, connection, owner, data):
        profile = self.profile(owner, data.profile_id)
        self.version(profile, data.version)
        if len({item.record_id for item in data.items}) != len(data.items):
            raise DomainError('INVALID_REQUEST', '批量归档不能包含重复房源。', 422)
        for item in data.items:
            record = self.owned_record(owner, item.record_id)
            self.version(record, item.version)
            if record['list_status'] != 'candidate':
                raise DomainError('INVALID_STATE', '只能归档候选，批量操作已回滚。')
        for item in data.items:
            connection.execute("UPDATE records SET list_status='archived',version=version+1 WHERE record_id=?", (item.record_id,))
        return {'records': [self.record(owner, item.record_id) for item in data.items]}

    def archive_all(self, connection, owner, data):
        profile = self.profile(owner, data.profile_id)
        self.version(profile, data.version)
        changed = connection.execute("UPDATE records SET list_status='archived',version=version+1 WHERE profile_id=? AND list_status='candidate'", (profile['profile_id'],)).rowcount
        return {'archived_count': changed}

    def restore(self, connection, owner, record_id, data):
        row = self.owned_record(owner, record_id)
        self.profile(owner, data.profile_id)
        self.version(row, data.version)
        if row['list_status'] != 'archived':
            raise DomainError('INVALID_STATE', '只有已归档候选可重新接受。')
        connection.execute("UPDATE records SET list_status='candidate',version=version+1 WHERE record_id=?", (record_id,))
        return self.record(owner, record_id)

    def report(self, owner, report_id):
        row = self.db.one('SELECT * FROM reports WHERE report_id=?', (report_id,))
        if not row:
            raise DomainError('NOT_FOUND', '报告不存在。', 404)
        self.owned_record(owner, row['record_id'])
        content=decode(row['content'])
        return {'schema_version': 1, **{key: row[key] for key in ('report_id', 'record_id', 'kind', 'generated_at')},
                'content':content,'report_sections':report_sections(content) if row['kind']=='full' else {},'evidence_sources':self.evidence_sources(row['record_id'],content.get('evidence_ids')),
                'completeness_summary':quality_summary(content.get('data_completeness'),content.get('missing'))}

    def evidence_sources(self, record_id, ids=None):
        rows=self.db.all('SELECT e.evidence_id,e.locator,e.uses,p.note_id FROM evidence e LEFT JOIN posts p ON p.post_key=e.post_key WHERE e.record_id=? ORDER BY e.evidence_id',(record_id,))
        original=self.db.one('SELECT p.note_id FROM records r JOIN posts p ON p.post_key=r.post_key WHERE r.record_id=?',(record_id,))['note_id']
        rows_by_id={r['evidence_id']:r for r in rows};order=list(dict.fromkeys(ids if ids is not None else rows_by_id));result={}
        for eid in order:
            row=rows_by_id.get(eid)
            if not row:continue
            kind=decode(row['locator']).get('type');main=row['note_id']==original
            label={'author_profile':'作者昵称与简介','history_title':'作者历史标题'}.get(kind)
            if kind in ('body','comment'):
                prefix='原帖' if main else ('偏好专项帖' if 'preference' in decode(row['uses']) and 'community' not in decode(row['uses']) else '小区调查帖')
                label=prefix+('正文' if kind=='body' else '评论')
            number=len(result)+1;result[eid]={'number':number,'source_kind':kind,'is_original':main,'label':'证据'+str(number)+'：'+(label or '原始资料')}
        return result

    def evidence(self, owner, evidence_id):
        row = self.db.one('SELECT * FROM evidence WHERE evidence_id=?', (evidence_id,))
        if not row:
            raise DomainError('NOT_FOUND', '证据不存在。', 404)
        self.owned_record(owner, row['record_id'])
        post = self.db.one('SELECT note_id,url FROM posts WHERE post_key=?', (row['post_key'],)) if row['post_key'] else None
        comment = self.db.one('SELECT comment_id FROM comments WHERE comment_key=?', (row['comment_key'],)) if row['comment_key'] else None
        author = self.db.one('SELECT author_id FROM authors WHERE author_key=?', (row['author_key'],)) if row['author_key'] else None
        return {'schema_version': 1, 'evidence_id': evidence_id, 'record_id': row['record_id'], 'excerpt': row['excerpt'],
                'uses': decode(row['uses']), 'source': {'note_id': post['note_id'] if post else None,
                'url': post['url'] if post else decode(row['locator']).get('url') or ('https://www.xiaohongshu.com/user/profile/' + author['author_id'] if author else None), 'comment_id': comment['comment_id'] if comment else None,
                'author_id': author['author_id'] if author else None, 'locator': decode(row['locator'])}}

    def viewing(self, owner, record_id):
        record = self.record(owner, record_id)
        if not record['full_report_id']:
            raise DomainError('INVALID_STATE', '完整报告尚未生成，不能形成现场风险清单。')
        report_view=self.report(owner,record['full_report_id']);report=report_view['content']
        row = self.db.one('SELECT * FROM viewing WHERE record_id=?', (record_id,))
        general = decode(row['general_checks']) if row else {item['key']: False for item in GENERAL_ITEMS}
        risks = decode(row['risk_checks']) if row else {item['risk_id']: False for item in report['risks']}
        return {'schema_version': 1, 'record_id': record_id, 'version': row['version'] if row else 0,
                'evidence_sources':report_view['evidence_sources'],
                'conclusion': row['conclusion'] if row else 'undecided', 'summary': row['summary'] if row else '',
                'followup': row['followup'] if row else '', 'saved_at': row['saved_at'] if row else None,
                'general_checks': general, 'risk_checks': risks,
                'progress': {'done': sum(general.values()) + sum(risks.values()), 'total': len(risks) + 12},
                'template': {'template_version': TEMPLATE_VERSION, 'general_items': GENERAL_ITEMS, 'risk_items': report['risks']}}

    def save_viewing(self, connection, owner, record_id, data):
        self.profile(owner, data.profile_id)
        current = self.viewing(owner, record_id)
        self.version(current, data.version)
        if data.template_version != TEMPLATE_VERSION or set(data.general_checks) != set(current['general_checks']) or set(data.risk_checks) != set(current['risk_checks']):
            raise DomainError('VALIDATION_FAILED', '现场模板或风险key不完整，未保存任何字段。', 422)
        connection.execute('INSERT INTO viewing VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET conclusion=excluded.conclusion,summary=excluded.summary,followup=excluded.followup,general_checks=excluded.general_checks,risk_checks=excluded.risk_checks,version=excluded.version,saved_at=excluded.saved_at',
                           (record_id, data.conclusion, data.summary, data.followup, encode(data.general_checks), encode(data.risk_checks), data.version + 1, now()))
        return {'viewing': self.viewing(owner, record_id), 'record': self.record(owner, record_id)}
